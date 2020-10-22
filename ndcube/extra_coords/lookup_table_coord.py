import copy
from functools import partial
from numbers import Integral

import astropy.units as u
import gwcs
import gwcs.coordinate_frames as cf
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.modeling import models
from astropy.modeling.models import tabular_model
from astropy.time import Time
from astropy.wcs.wcsapi.sliced_low_level_wcs import sanitize_slices

__all__ = ['LookupTableCoord']


class LookupTableCoord:
    """
    A class representing world coordinates described by a lookup table.

    This class takes an input in the form of a lookup table (can be many
    different array-like types) and holds the building blocks (transform and
    frame) to generate a `gwcs.WCS` object.

    This can be used as a way to generate gWCS objects based on lookup tables,
    however, it lacks some of the flexibility of doing this manually.

    Parameters
    ----------
    lookup_tables : `object`
        The lookup tables. If more than one lookup table is specified, it
        should represent one physical coordinate type, i.e "spatial". They must
        all be the same type, shape and unit.
    """

    def __init__(self, *lookup_tables, mesh=True, names=None, physical_types=None):
        self.delayed_models = []
        self.frames = []

        if lookup_tables:
            lt0 = lookup_tables[0]
            if not all(isinstance(lt, type(lt0)) for lt in lookup_tables):
                raise TypeError("All lookup tables must be the same type")

            if not all(lt0.shape == lt.shape for lt in lookup_tables):
                raise ValueError("All lookup tables must have the same shape")

            type_map = {
                u.Quantity: self._from_quantity,
                Time: self._from_time,
                SkyCoord: self._from_skycoord
            }
            delayed_model, frame = type_map[type(lt0)](lookup_tables,
                                                       mesh=mesh,
                                                       names=names,
                                                       physical_types=physical_types)
            self.delayed_models = [delayed_model]
            self.frames = [frame]

    def __str__(self):
        return f"{self.frames=} {self.delayed_models=}"

    def __repr__(self):
        return f"{object.__repr__(self)}\n{self}"

    def __and__(self, other):
        if not isinstance(other, LookupTableCoord):
            raise TypeError(
                "Can only concatenate LookupTableCoord objects with other LookupTableCoord objects.")

        new_lutc = copy.copy(self)
        new_lutc.delayed_models += other.delayed_models
        new_lutc.frames += other.frames

        # We must now re-index the frames so that they align with the composite frame
        ind = 0
        for f in new_lutc.frames:
            new_ind = ind + f.naxes
            f._axes_order = tuple(range(ind, new_ind))
            ind = new_ind

        return new_lutc

    def __getitem__(self, item):
        item = sanitize_slices(item, self.ndim)
        if not isinstance(item, (list, tuple)):
            item = (item,)

        ind = 0
        new_dmodels = []
        new_frames = []
        for dmodel, frame in zip(self.delayed_models, self.frames):
            model = dmodel()
            n_axes = model.n_inputs
            sub_items = tuple(item[i] for i in range(ind, ind + n_axes))
            ind += n_axes

            # If all the slice elements are ints then we are dropping this model
            if not all(isinstance(it, Integral) for it in sub_items):
                new_dmodels.append(dmodel[sub_items])
                new_frames.append(frame)

        if not new_dmodels:
            return

        new_lutc = type(self)()
        new_lutc.delayed_models = new_dmodels
        new_lutc.frames = new_frames
        return new_lutc

    @property
    def ndim(self):
        ndim = 0
        for frame in self.frames:
            ndim += len(frame.axes_order)
        return ndim

    @property
    def model(self):
        model = self.delayed_models[0]()
        for m2 in self.delayed_models[1:]:
            model = model & m2()
        return model

    @property
    def frame(self):
        if len(self.frames) == 1:
            return self.frames[0]
        else:
            return cf.CompositeFrame(self.frames)

    @property
    def wcs(self):
        return gwcs.WCS(forward_transform=self.model,
                        input_frame=self._generate_generic_frame(self.model.n_inputs, u.pix),
                        output_frame=self.frame)

    @staticmethod
    def generate_tabular(lookup_table, interpolation='linear', points_unit=u.pix, **kwargs):
        if not isinstance(lookup_table, u.Quantity):
            raise TypeError("lookup_table must be a Quantity.")

        ndim = lookup_table.ndim
        TabularND = tabular_model(ndim, name=f"Tabular{ndim}D")

        # The integer location is at the centre of the pixel.
        points = [(np.arange(size) - 0) * points_unit for size in lookup_table.shape]
        if len(points) == 1:
            points = points[0]

        kwargs = {
            'bounds_error': False,
            'fill_value': np.nan,
            'method': interpolation,
            **kwargs
        }

        return TabularND(points, lookup_table, **kwargs)

    @classmethod
    def _generate_compound_model(cls, *lookup_tables, mesh=True):
        """
        Takes a set of quantities and returns a ND compound model.
        """
        model = cls.generate_tabular(lookup_tables[0])
        for lt in lookup_tables[1:]:
            model = model & cls.generate_tabular(lt)

        if mesh:
            return model

        # If we are not meshing the inputs duplicate the inputs across all models
        mapping = list(range(lookup_tables[0].ndim)) * len(lookup_tables)
        return models.Mapping(mapping) | model

    @staticmethod
    def _generate_generic_frame(naxes, unit, names=None, physical_types=None):
        """
        Generate a simple frame, where all axes have the same type and unit.
        """
        axes_order = tuple(range(naxes))

        name = None
        axes_type = "CUSTOM"

        if isinstance(unit, (u.Unit, u.IrreducibleUnit)):
            unit = tuple([unit] * naxes)

        if all([u.m.is_equivalent(un) for un in unit]):
            axes_type = "SPATIAL"

        if all([u.pix.is_equivalent(un) for un in unit]):
            name = "PixelFrame"
            axes_type = "PIXEL"

        axes_type = tuple([axes_type] * naxes)

        return cf.CoordinateFrame(naxes, axes_type, axes_order, unit=unit,
                                  axes_names=names, name=name, axis_physical_types=physical_types)

    def _from_time(self, lookup_tables, mesh=False, names=None, physical_types=None, **kwargs):
        if len(lookup_tables) > 1:
            raise ValueError("Can only parse one time lookup table.")

        time = lookup_tables[0]
        deltas = (time[1:] - time[0]).to(u.s)
        deltas = deltas.insert(0, 0)

        def _generate_time_lookup(deltas):
            return self._model_from_quantity((deltas,))

        frame = cf.TemporalFrame(lookup_tables[0][0], unit=u.s, axes_names=names, name="TemporalFrame")
        return DelayedLookupTable(deltas, _generate_time_lookup, mesh), frame

    def _from_skycoord(self, lookup_tables, mesh=False, names=None, physical_types=None, **kwargs):
        if len(lookup_tables) > 1:
            raise ValueError("Can only parse one SkyCoord lookup table.")

        def _generate_skycoord_lookup(components):
            return self._model_from_quantity(components, mesh=mesh)

        sc = lookup_tables[0]
        components = tuple(getattr(sc.data, comp) for comp in sc.data.components)
        ref_frame = sc.frame.replicate_without_data()
        units = list(c.unit for c in components)

        # TODO: Currently this limits you to 2D due to gwcs#120
        frame = cf.CelestialFrame(reference_frame=ref_frame,
                                  unit=units,
                                  axes_names=names,
                                  axis_physical_types=physical_types,
                                  name="CelestialFrame")

        return DelayedLookupTable(components, _generate_skycoord_lookup, mesh), frame

    def _model_from_quantity(self, lookup_tables, mesh=False):
        if len(lookup_tables) > 1:
            if not all((isinstance(x, u.Quantity) for x in lookup_tables)):
                raise TypeError("Can only parse a list or tuple of u.Quantity objects.")

            return self._generate_compound_model(*lookup_tables, mesh=mesh)

        return self.generate_tabular(lookup_tables[0])

    def _from_quantity(self, lookup_tables, mesh=False, names=None, physical_types=None):
        if not all(lt.unit.is_equivalent(lt[0].unit) for lt in lookup_tables):
            raise u.UnitsError("All lookup tables must have equivalent units.")

        unit = u.Quantity(lookup_tables).unit

        frame = self._generate_generic_frame(len(lookup_tables), unit, names, physical_types)

        return DelayedLookupTable(lookup_tables, partial(self._model_from_quantity, mesh=mesh), mesh), frame


class DelayedLookupTable:
    """
    A wrapper to create a lookup table model on demand.
    """

    def __init__(self, lookup_table, model_function, mesh=True):
        self.lookup_table = lookup_table
        self.model_function = model_function
        self.mesh = mesh

    def __call__(self):
        return self.model_function(self.lookup_table)

    def __getitem__(self, item):
        if isinstance(self.lookup_table, tuple):
            if self.mesh:
                assert len(item) == len(self.lookup_table)
                newlt = tuple(lt[sub_item] for lt, sub_item in zip(self.lookup_table, item))
            else:
                newlt = tuple(lt[item] for lt in self.lookup_table)
        else:
            newlt = self.lookup_table[item]

        return type(self)(newlt, self.model_function)

    def __str__(self):
        return f"DelayedLookupTable(lookup_table={self.lookup_table}"

    def __repr__(self):
        return f"{object.__repr__(self)}\n{str(self)}"
