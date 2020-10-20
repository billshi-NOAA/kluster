import os
import numpy as np
import json
import xarray as xr
import zarr
from dask.distributed import wait
from xarray.core.combine import _infer_concat_order_from_positions, _nested_combine

from HSTB.kluster.dask_helpers import DaskProcessSynchronizer


class ZarrWrite:
    """
    Class for handling writing xarray data to Zarr.  I started off using the xarray to_zarr functions, but I found
    that they could not handle changes in size/distributed writes very well, so I came up with my own.  This class
    currently supports:

      1. writing to zarr from dask map function, see distrib_zarr_write
      2. writing data with a larger expand dimension than currently exists in zarr (think new data has more beams)
      3. writing new variable to existing zarr data store (must match existing data dimensions)
      4. appending to existing zarr by filling in the last zarr chunk with data and then writing new chunks (only last
                chunk of zarr array is allowed to not be of length equal to zarr chunk size)
    """
    def __init__(self, zarr_path, desired_chunk_shape, append_dim='time', expand_dim='beam', float_no_data_value=np.nan,
                 int_no_data_value=999, sync=None):
        """
        Initialize zarr write class

        Parameters
        ----------
        zarr_path: str, full file path to where you want the zarr data store to be written to
        desired_chunk_shape: dict, keys are dimension names, vals are the chunk size for that dimension
        append_dim: str, dimension name that you are appending to (generally time)
        expand_dim: str, dimension name that you need to expand if necessary (generally beam)
        float_no_data_value: float, no data value for variables that are dtype float
        int_no_data_value: int, no data value for variables that are dtype int

        """
        self.zarr_path = zarr_path
        self.desired_chunk_shape = desired_chunk_shape
        self.append_dim = append_dim
        self.expand_dim = expand_dim
        self.float_no_data_value = float_no_data_value
        self.int_no_data_value = int_no_data_value

        self.sync = sync
        self.rootgroup = None
        self.zarr_array_names = []

        self.merge_chunks = False

        self.open()

    def open(self):
        """
        Open the zarr data store, will create a new one if it does not exist.  Get all the existing array names.
        """
        self.rootgroup = zarr.open(self.zarr_path, mode='a', synchronizer=self.sync)
        self.get_array_names()

    def get_array_names(self):
        """
        Get all the existing array names as a list of strings and set self.zarr_array_names with that list
        """
        self.zarr_array_names = [t for t in self.rootgroup.array_keys()]

    def _attributes_only_unique_profile(self, attrs):
        """
        Given attribute dict from dataset (attrs) retain only unique sound velocity profiles

        Parameters
        ----------
        attrs: dict, input attribution from converted dataset

        Returns
        -------
        attrs: dict, attrs with only unique sv profiles

        """
        try:
            new_profs = [x for x in attrs.keys() if x[0:7] == 'profile']
            curr_profs = [x for x in self.rootgroup.attrs.keys() if x[0:7] == 'profile']
            current_vals = [self.rootgroup.attrs[p] for p in curr_profs]
            for prof in new_profs:
                val = attrs[prof]
                if val in current_vals:
                    attrs.pop(prof)
        except:
            pass
        return attrs

    def _attributes_only_unique_settings(self, attrs):
        """
        Given attribute dict from dataset (attrs) retain only unique settings dicts

        Parameters
        ----------
        attrs: dict, input attribution from converted dataset

        Returns
        -------
        attrs: dict, attrs with only unique settings dicts

        """
        try:
            new_settings = [x for x in attrs.keys() if x[0:8] == 'settings']
            curr_settings = [x for x in self.rootgroup.attrs.keys() if x[0:8] == 'settings']
            current_vals = [self.rootgroup.attrs[p] for p in curr_settings]
            for sett in new_settings:
                val = attrs[sett]
                if val in current_vals:
                    attrs.pop(sett)
        except:
            pass
        return attrs

    def _attributes_only_unique_xyzrph(self, attrs):
        """
        Given attribute dict from dataset (attrs) retain only unique xyzrph constructs

        xyzrph is constructed in processing as the translated settings

        Parameters
        ----------
        attrs: dict, input attribution from converted dataset

        Returns
        -------
        attrs: dict, attrs with only unique xyzrph timestamped records

        """
        try:
            new_xyz = attrs['xyzrph']
            new_tstmps = list(new_xyz[list(new_xyz.keys())[0]].keys())
            curr_xyz = self.rootgroup.attrs['xyzrph']
            curr_tstmps = list(curr_xyz[list(curr_xyz.keys())[0]].keys())

            curr_vals = []
            for tstmp in curr_tstmps:
                curr_vals.append([curr_xyz[x][tstmp] for x in curr_xyz])
            for tstmp in new_tstmps:
                new_val = [new_xyz[x][tstmp] for x in new_xyz]
                if new_val in curr_vals:
                    for ky in new_xyz:
                        new_xyz[ky].pop(tstmp)
            if not new_xyz[list(new_xyz.keys())[0]]:
                attrs.pop('xyzrph')
        except:
            pass
        return attrs

    def write_attributes(self, attrs):
        """
        Write out attributes to the zarr data store

        Parameters
        ----------
        attrs: dict, attributes associated with this zarr rootgroup

        """
        if attrs is not None:
            attrs = self._attributes_only_unique_profile(attrs)
            attrs = self._attributes_only_unique_settings(attrs)
            attrs = self._attributes_only_unique_xyzrph(attrs)
            _my_xarr_to_zarr_writeattributes(self.rootgroup, attrs)

    def _check_merge(self, input_xarr):
        """
        A merge is when you have an existing zarr datastore and you want to write a new variable (think array) to it
        that does not currently exist in the datastore.  You need to ensure that the existing dimensions match this
        new variable dimensions.  We do that here.

        Parameters
        ----------
        input_xarr: xarray Dataset/DataArray, xarray object that we want to write to the zarr data store

        """
        if self.append_dim in self.rootgroup:
            if (input_xarr[self.append_dim][0] >= np.min(self.rootgroup[self.append_dim])) and (
                    input_xarr[self.append_dim][-1] <= np.max(self.rootgroup[self.append_dim])):
                return True  # append_dim (usually time) range exists in rootgroup, qualifies for merge
            else:
                return False  # first/last time isn't the same, must not be a merge
        else:
            return False  # zarr didn't have the append dimension, must not be a merge

    def _check_fix_rootgroup_expand_dim(self, xarr):
        """
        Check if this xarr is greater in the exand dimension (probably beam) than the existing rootgroup beam array.  If it is,
        we'll need to expand the rootgroup to cover the max beams of the xarr.

        Parameters
        ----------
        xarr: xarray Dataset, data that we are trying to write to rootgroup

        Returns
        -------
        bool, if True expand the rootgroup expand dimension

        """
        if (self.expand_dim in self.rootgroup) and (self.expand_dim in xarr):
            last_expand = self.rootgroup[self.expand_dim].size
            if last_expand < xarr[self.expand_dim].shape[0]:
                return True  # last expand dim isn't long enough, need to fix the chunk
            else:
                return False  # there is a chunk there, but it is of size equal to desired
        else:
            return False  # first write

    def _get_arr_nodatavalue(self, arr_dtype):
        """
        Given the dtype of the array, determine the appropriate no data value.  Fall back on empty string if not int or
        float.

        Parameters
        ----------
        arr_dtype: numpy dtype, dtype of input array

        Returns
        -------
        no data value, one of [self.float_no_data_value, self.int_no_data_value, '']

        """
        isfloat = np.issubdtype(arr_dtype, np.floating)
        if isfloat:
            nodata = self.float_no_data_value
        else:
            isint = np.issubdtype(arr_dtype, np.integer)
            if isint:
                nodata = self.int_no_data_value
            else:
                nodata = ''
        return nodata

    def fix_rootgroup_expand_dim(self, xarr):
        """
        Once we've determined that the xarr Dataset expand_dim is greater than the rootgroup expand_dim, expand the
        rootgroup expand_dim to match the xarr.  Fill the empty space with the appropriate no data value.

        Parameters
        ----------
        xarr: xarray Dataset, data that we are trying to write to rootgroup

        """
        curr_expand_dim_size = self.rootgroup[self.expand_dim].size
        for var in self.zarr_array_names:
            newdat = None
            newshp = None
            if var == self.expand_dim:
                newdat = np.arange(xarr[self.expand_dim].shape[0])
                newshp = xarr[self.expand_dim].shape
            elif self.rootgroup[var].ndim >= 2:
                if self.rootgroup[var].shape[1] == curr_expand_dim_size:  # you found an array with a beam dimension
                    nodata_value = self._get_arr_nodatavalue(self.rootgroup[var].dtype)
                    newdat = self._inflate_expand_dim(self.rootgroup[var], xarr[self.expand_dim].shape[0], nodata_value)
                    newshp = list(self.rootgroup[var].shape)
                    newshp[1] = xarr[self.expand_dim].shape[0]
                    newshp = tuple(newshp)
            if newdat is not None:
                self.rootgroup[var].resize(newshp)
                self.rootgroup[var][:] = newdat

    def _inflate_expand_dim(self, input_arr, expand_dim_size, nodata):
        """
        Take in the rootgroup and expand the beam dimension to the expand_dim_size, filling the empty space with the
        nodata value.

        Parameters
        ----------
        input_arr: numpy like object, includes zarr.core.Array and xarray.core.dataarray.DataArray, data that we want
                   to expand to match the expand dim size
        expand_dim_size: int, size of the expand_dim (probably beam) that we need
        nodata: one of [self.float_no_data_value, self.int_no_data_value, '']

        Returns
        -------
        new_arr, input_arr with expanded beam dimension

        """
        if input_arr.ndim == 3:
            appended_data = np.full((input_arr.shape[0], expand_dim_size - input_arr.shape[1], input_arr.shape[2]), nodata)
        else:
            appended_data = np.full((input_arr.shape[0], expand_dim_size - input_arr.shape[1]), nodata)
        new_arr = np.concatenate((input_arr, appended_data), axis=1)
        return new_arr

    def correct_rootgroup_dims(self, xarr):
        """
        Correct for when the input xarray Dataset shape is greater than the rootgroup shape.  Most likely this is when
        the input xarray Dataset is larger in the beam dimension than the existing rootgroup arrays.

        Parameters
        ----------
        xarr: xarray Dataset, data that we are trying to write to rootgroup

        """
        if self._check_fix_rootgroup_expand_dim(xarr):
            self.fix_rootgroup_expand_dim(xarr)

    def _write_adjust_max_beams(self, startingshp):
        """
        The first write in appending to an existing zarr data store will resize that zarr array to the expected size
        of the new data + old data.  We provide the expected shape when we write, but that shape is naive to the
        beam dimension of the existing data.  Here we correct that.

        Parameters
        ----------
        startingshp: tuple, expected shape of the appended data + existing data

        Returns
        -------
        startingshp: tuple, same shape, but with beam dimension corrected for the existing data

        """
        if len(startingshp) >= 2:
            current_max_beams = self.rootgroup['beam'].shape[0]
            startingshp = list(startingshp)
            startingshp[1] = current_max_beams
            startingshp = tuple(startingshp)
        return startingshp

    def _write_determine_shape(self, var, dims_of_arrays, finalsize):
        """
        Given the size information and dimension names for the given variable, determine the axis to append to and the
        expected shape for the rootgroup array.

        Parameters
        ----------
        var: str, name of the array, ex: 'beampointingangle'
        dims_of_arrays: dict, where keys are array names and values list of dims/shape.  Example:
                              'beampointingangle': [['time', 'sector', 'beam'], (5000, 3, 400)]
        finalsize: optional, int, if provided will resize zarr to the expected final size after all writes have been
                   performed.

        Returns
        -------
        timaxis: int, index of the time dimension
        timlength: int, length of the time dimension for the input xarray Dataset
        startingshp: tuple, desired shape for the rootgroup array, might be modified later for total beams if necessary.
                     if finalsize is None (the case when this is not the first write in a set of distributed writes)
                     this is still returned but not used.

        """
        if var in ['beam', 'xyz']:
            # only need time dim info for time dependent variables
            timaxis = None
            timlength = None
            startingshp = dims_of_arrays[var][1]
        else:
            # want to get the length of the time dimension, so you know which dim to append to
            timaxis = dims_of_arrays[var][0].index(self.append_dim)
            timlength = dims_of_arrays[var][1][timaxis]
            startingshp = tuple(
                finalsize if dims_of_arrays[var][1].index(x) == timaxis else x for x in dims_of_arrays[var][1])
        return timaxis, timlength, startingshp

    def _write_existing_rootgroup(self, xarr, data_loc_copy, var, dims_of_arrays, chunksize, timlength, timaxis,
                                  startingshp):
        """
        A slightly different operation than _write_new_dataset_rootgroup.  To write to an existing rootgroup array,
        we use the data_loc as an index and create a new zarr array from the xarray Dataarray.  The data_loc is only
        used if the var is a time based array.

        Parameters
        ----------
        xarr: xarray Dataset, data to write to zarr
        data_loc_copy: list, [start time index, end time index] for xarr, ex: [0,1000] if xarr time dimension is
                       1000 long.
        var: str, variable name
        dims_of_arrays: dict, where keys are array names and values list of dims/shape.  Example:
                        'beampointingangle': [['time', 'sector', 'beam'], (5000, 3, 400)]
        chunksize: tuple, chunk shape used to create the zarr array
        timlength: int, length of the time dimension for the input xarray Dataset
        timaxis: int, index of the time dimension
        startingshp: tuple, desired shape for the rootgroup array, might be modified later for total beams if necessary.
                     if finalsize is None (the case when this is not the first write in a set of distributed writes)
                     this is still returned but not used.

        """
        # array to be written
        newarr = zarr.array(xarr[var].values, shape=dims_of_arrays[var][1], chunks=chunksize)

        # the last write will often be less than the block size.  This is allowed in the zarr store, but we
        #    need to correct the index for it.
        if timlength != data_loc_copy[1] - data_loc_copy[0]:
            data_loc_copy[1] = data_loc_copy[0] + timlength

        # location for new data, assume constant chunksize (as we are doing this outside of this function)
        chunk_time_range = slice(data_loc_copy[0], data_loc_copy[1])
        # use the chunk_time_range for writes unless this variable is a non-time dim array (beam for example)
        chunk_idx = tuple(
            chunk_time_range if dims_of_arrays[var][1].index(i) == timaxis else slice(0, i) for i in
            dims_of_arrays[var][1])
        if startingshp is not None and var != 'tx':
            startingshp = self._write_adjust_max_beams(startingshp)
            self.rootgroup[var].resize(startingshp)

        self.rootgroup[var][chunk_idx] = newarr

    def _write_new_dataset_rootgroup(self, xarr, var, dims_of_arrays, chunksize, startingshp):
        """
        Create a new rootgroup array from the input xarray Dataarray.  Use startingshp to resize the array to the
        expected shape of the array after ALL writes.  This must be the first write if there are multiple distributed
        writes.

        Parameters
        ----------
        xarr: xarray Dataset, data to write to zarr
        var: str, variable name
        dims_of_arrays: dict, where keys are array names and values list of dims/shape.  Example:
                        'beampointingangle': [['time', 'sector', 'beam'], (5000, 3, 400)]
        chunksize: tuple, chunk shape used to create the zarr array
        startingshp: tuple, desired shape for the rootgroup array, might be modified later for total beams if necessary.
                     if finalsize is None (the case when this is not the first write in a set of distributed writes)
                     this is still returned but not used.

        """
        newarr = self.rootgroup.create_dataset(var, shape=dims_of_arrays[var][1], chunks=chunksize,
                                               dtype=xarr[var].dtype, synchronizer=self.sync,
                                               fill_value=self._get_arr_nodatavalue(xarr[var].dtype))
        newarr[:] = xarr[var].values
        newarr.resize(startingshp)

    def write_to_zarr(self, xarr, attrs, dataloc, finalsize=None, merge=False):
        """
        Take the input xarray Dataset and write each variable as arrays in a zarr rootgroup.  Write the attributes out
        to the rootgroup as well.  Dataloc determines the index the incoming data is written to.  A new write might
        have a dataloc of [0,100], if the time dim of the xarray Dataset was 100 long.  A write to an existing zarr
        rootgroup might have a dataloc of [300,400] if we were appending to it.

        Parameters
        ----------
        xarr: xarray Dataset, data to write to zarr
        attrs: dict, attributes we want written to zarr rootgroup
        dataloc: list, [start time index, end time index] for xarr, ex: [0,1000] if xarr time dimension is 1000 long.
        finalsize: optional, int, if provided will resize zarr to the expected final size after all writes have been
                   performed.  (We need to resize the zarr for that expected size before writing)
        merge: bool, if True, you are passing a Dataset containing a new variable that is not in an existing dataset.
               Ex: xarr contains variable 'corr_pointing_angle' and that variable is not in the rootgroup

        Returns
        -------
        zarr_path: str, path to zarr data store

        """
        if merge and not self._check_merge(xarr):
            raise ValueError('write_to_zarr: Unable to merge, time not found in existing data store.')
        if finalsize is not None:
            self.correct_rootgroup_dims(xarr)
        self.get_array_names()
        dims_of_arrays = _my_xarr_to_zarr_build_arraydimensions(xarr)
        self.write_attributes(attrs)

        for var in dims_of_arrays:
            already_written = var in self.zarr_array_names
            if merge and var in [self.append_dim, 'beam']:  # Merge does not change time/beam dimension, it only adds new variable
                continue
            if var in ['beam', 'xyz'] and already_written:
                # no append_dim (usually time) component to these arrays
                # You should only have to write this once, if beam dim expands, correct_rootgroup_dims handles it
                continue

            timaxis, timlength, startingshp = self._write_determine_shape(var, dims_of_arrays, finalsize)
            chunksize = self.desired_chunk_shape[var]
            data_loc_copy = dataloc.copy()

            # shape is extended on append.  chunks will always be equal to shape, as each run of this function will be
            #     done on one chunk of data by one worker
            if var in self.zarr_array_names:
                if finalsize is not None:  # appending data, first write contains the final shape of the data
                    self._write_existing_rootgroup(xarr, data_loc_copy, var, dims_of_arrays, chunksize, timlength,
                                                   timaxis, startingshp)
                else:
                    self._write_existing_rootgroup(xarr, data_loc_copy, var, dims_of_arrays, chunksize, timlength,
                                                   timaxis, None)
            else:
                self._write_new_dataset_rootgroup(xarr, var, dims_of_arrays, chunksize, startingshp)

            # _ARRAY_DIMENSIONS is used by xarray for connecting dimensions with zarr arrays
            self.rootgroup[var].attrs['_ARRAY_DIMENSIONS'] = dims_of_arrays[var][0]
        return self.zarr_path


def _distrib_zarr_write_convenience(zarr_path, xarr, attrs, desired_chunk_shape, dataloc, append_dim='time',
                                    finalsize=None, merge=False, sync=None):
    """
    Convenience function for writing with ZarrWrite

    Parameters
    ----------
    zarr_path: str, path to zarr data store
    xarr: xarray Dataset, data to write to zarr
    attrs: dict, attributes we want written to zarr rootgroup
    desired_chunk_shape: dict, variable name: chunk size as tuple, for each variable in the input xarr
    dataloc: list, [start time index, end time index] for xarr, ex: [0,1000] if xarr time dimension is 1000 long.
    finalsize: optional, int, if provided will resize zarr to the expected final size after all writes have been
               performed.  (We need to resize the zarr for that expected size before writing)
    merge: bool, if True, you are passing a Dataset containing a new variable that is not in an existing dataset.  Ex:
           xarr contains variable 'corr_pointing_angle' and that variable is not in the rootgroup
    sync: synchronizer for write, generally a dask.distributed.Lock based sync

    Returns
    -------
    zarr_path: str, path to zarr data store

    """
    zw = ZarrWrite(zarr_path, desired_chunk_shape, append_dim=append_dim, sync=sync)
    zarr_path = zw.write_to_zarr(xarr, attrs, dataloc, finalsize=finalsize, merge=merge)
    return zarr_path


def distrib_zarr_write(zarr_path, xarrays, attributes, chunk_sizes, data_locs, sync, client, append_dim='time',
                       merge=False, skip_dask=False):
    """
    A function for using the ZarrWrite class to write data to disk.  xarr and attrs are written to the datastore at
    zarr_path.  We use the function (and not the class directly) in Dask when we map it across all the workers.  Dask
    serializes data when mapping, so passing classes causes issues.

    Currently we wait between each write.  This seems to deal with the occassional permissions error that pops up
    when letting dask write in parallel.  Maybe we aren't using the sync object correctly?  Needs more testing.

    Parameters
    ----------
    zarr_path: str, path to zarr data store
    xarrays: list, xarray Datasets, data to write to zarr
    attributes: dict, attributes we want written to zarr rootgroup
    chunk_sizes: dict, variable name: chunk size as tuple, for each variable in the input xarr
    data_locs: list of lists, [start time index, end time index] for xarr, ex: [0,1000] if xarr time dimension is 1000
               long.
    sync: synchronizer for write, generally a dask.distributed.Lock based sync
    client: dask.distributed.Client, the client we are submitting the tasks to
    append_dim: str, dimension name that you are appending to (generally time)
    merge: bool, if True, you are passing a Dataset containing a new variable that is not in an existing dataset.  Ex:
           xarr contains variable 'corr_pointing_angle' and that variable is not in the rootgroup

    Returns
    -------
    list, futures objects containing the path to the zarr rootgroup.

    """
    if skip_dask:
        for cnt, arr in enumerate(xarrays):
            if cnt == 0:
                futs = [_distrib_zarr_write_convenience(zarr_path, arr, attributes, chunk_sizes, data_locs[cnt],
                        append_dim=append_dim, finalsize=data_locs[-1][1], merge=merge, sync=sync)]
            else:
                futs.append([_distrib_zarr_write_convenience(zarr_path, xarrays[cnt], None, chunk_sizes, data_locs[cnt],
                             append_dim=append_dim, merge=merge, sync=sync)])
    else:
        futs = [client.submit(_distrib_zarr_write_convenience, zarr_path, xarrays[0], attributes, chunk_sizes, data_locs[0],
                              append_dim=append_dim, finalsize=data_locs[-1][1], merge=merge, sync=sync)]
        wait(futs)
        if len(xarrays) > 1:
            for i in range(len(xarrays) - 1):
                futs.append(client.submit(_distrib_zarr_write_convenience, zarr_path, xarrays[i + 1], None, chunk_sizes,
                                          data_locs[i + 1], append_dim=append_dim, merge=merge, sync=sync))
                wait(futs)
    return futs


def my_open_mfdataset(paths, chnks=None, concat_dim='time', compat='no_conflicts', data_vars='all',
                      coords='different', join='outer'):
    """
    Trying to address the limitations of the existing xr.open_mfdataset function.  This is my modification using
    the existing function and tweaking to resolve the issues i've found.

    (see https://github.com/pydata/xarray/blob/master/xarray/backends/api.py)

    Current issues with open_mfdataset (1/8/2020):
    1. open_mfdataset only uses the attrs from the first nc file
    2. open_mfdataset will not run with parallel=True or with the distributed.LocalCluster running
    3. open_mfdataset infers time order from position.  (I could just sort outside of the function, but i kinda
         like it this way anyway.  Also a re-indexing would probably resolve this.)

    Only resolved item=1 so far.  See https://github.com/pydata/xarray/issues/3684

    Returns
    -------
    combined: Xarray Dataset - with attributes, variables, dimensions of combined netCDF files.  Returns dask
            arrays, compute to access local numpy array.

    """
    # ensure file paths are valid
    pth_chk = np.all([os.path.exists(x) for x in paths])
    if not pth_chk:
        raise ValueError('Check paths supplied to function.  Some/all files do not exist.')

    # sort by filename index, e.g. rangeangle_0.nc, rangeangle_1.nc, rangeangle_2.nc, etc.
    idxs = [int(os.path.splitext(os.path.split(x)[1])[0].split('_')[1]) for x in paths]
    sortorder = sorted(range(len(idxs)), key=lambda k: idxs[k])

    # sort_paths are the paths in sorted order by the filename index
    sort_paths = [paths[p] for p in sortorder]

    # build out the arugments for the nested combine
    if isinstance(concat_dim, (str, xr.DataArray)) or concat_dim is None:
        concat_dim = [concat_dim]
    combined_ids_paths = _infer_concat_order_from_positions(sort_paths)
    ids, paths = (list(combined_ids_paths.keys()), list(combined_ids_paths.values()))
    if chnks is None:
        chnks = {}

    datasets = [xr.open_dataset(p, engine='netcdf4', chunks=chnks, lock=None, autoclose=None) for p in paths]

    combined = _nested_combine(datasets, concat_dims=concat_dim, compat=compat, data_vars=data_vars,
                               coords=coords, ids=ids, join=join)
    combined.attrs = combine_xr_attributes(datasets)
    return combined


def xarr_to_netcdf(xarr, pth, fname, attrs, idx=None):
    """
    Takes in an xarray Dataset and pushes it to netcdf.
    For use with the output from combine_xarrs and/or _sequential_to_xarray

    Returns
    -------
    finalpth: str - path to the netcdf file

    """
    if idx is not None:
        finalpth = os.path.join(pth, os.path.splitext(fname)[0] + '_{}.nc'.format(idx))
    else:
        finalpth = os.path.join(pth, fname)

    if attrs is not None:
        xarr.attrs = attrs

    xarr.to_netcdf(path=finalpth, format='NETCDF4', engine='netcdf4')
    return finalpth


def xarr_to_zarr(xarr, attrs, outputpth, sync):
    """
    Takes in an xarray Dataset and pushes it to zarr store.

    Must be run once to generate new store.
    Successive runs append, see mode flag

    Returns
    -------
    finalpth: str - path to the zarr group

    """
    # grpname = str(datetime.now().strftime('%H%M%S%f'))
    if attrs is not None:
        xarr.attrs = attrs

    if not os.path.exists(outputpth):
        xarr.to_zarr(outputpth, mode='w-', compute=False)
    else:
        xarr.to_zarr(outputpth, mode='a', synchronizer=sync, compute=False, append_dim='time')

    return outputpth


def _my_xarr_to_zarr_build_arraydimensions(xarr):
    """
    Build out dimensions/shape of arrays in xarray into a dict so that we can use it with the zarr writer.

    Parameters
    ----------
    xarr: xarray Dataset, one chunk of the final range_angle/attitude/navigation xarray Dataset we are writing

    Returns
    -------
    dims_of_arrays: dict, where keys are array names and values list of dims/shape.  Example:
                    'beampointingangle': [['time', 'sector', 'beam'], (5000, 3, 400)]

    """
    dims_of_arrays = {}
    arrays_in_xarr = list(xarr.variables.keys())
    for arr in arrays_in_xarr:
        if xarr[arr].dims and xarr[arr].shape:  # only return arrays that have dimensions/shape
            dims_of_arrays[arr] = [xarr[arr].dims, xarr[arr].shape, xarr[arr].chunks]
    return dims_of_arrays


def _my_xarr_to_zarr_writeattributes(rootgroup, attrs):
    """
    Take the attributes generated with combine_xr_attributes and write them to the final datastore

    Parameters
    ----------
    rootgroup: zarr group, zarr datastore group for one of range_angle/attitude/navigation
    attrs: dict, dictionary of combined attributes from xarray datasets, None if no attributes exist

    """

    if attrs is not None:
        for att in attrs:

            # ndarray is not json serializable
            if isinstance(attrs[att], np.ndarray):
                attrs[att] = attrs[att].tolist()

            if att not in rootgroup.attrs:
                rootgroup.attrs[att] = attrs[att]
            else:
                if isinstance(attrs[att], list):
                    for sub_att in attrs[att]:
                        if sub_att not in rootgroup.attrs[att]:
                            rootgroup.attrs[att].append(attrs[att])
                elif isinstance(attrs[att], dict):
                    # have to load update and save to update dict attributes for some reason
                    dat = rootgroup.attrs[att]
                    dat.update(attrs[att])
                    rootgroup.attrs[att] = dat
                else:
                    rootgroup.attrs[att] = attrs[att]


def resize_zarr(zarrpth, finaltimelength=None):
    """
    Takes in the path to a zarr group and resizes the time dimension according to the provided finaltimelength

    Parameters
    ----------
    zarrpth: str, path to a zarr group on the filesystem
    finaltimelength: int, new length for the time dimension

    """
    # the last write will often be less than the block size.  This is allowed in the zarr store, but we
    #    need to correct the index for it.
    rootgroup = zarr.open(zarrpth, mode='r+')
    if finaltimelength is None:
        finaltimelength = np.count_nonzero(~np.isnan(rootgroup['time']))
    for var in rootgroup.arrays():
        if var[0] not in ['beam', 'sector']:
            varname = var[0]
            dims = rootgroup[varname].attrs['_ARRAY_DIMENSIONS']
            time_index = dims.index('time')
            new_shape = list(rootgroup[varname].shape)
            new_shape[time_index] = finaltimelength
            rootgroup[varname].resize(tuple(new_shape))


def combine_xr_attributes(datasets):
    """
    xarray open_mfdataset only retains the attributes of the first dataset.  We store profiles and installation
    parameters in datasets as they arise.  We need to combine the attributes across all datasets for our final
    dataset.

    Designed for the ping record, with filenames, survey identifiers, etc.  Will also accept min/max stats from navigation

    Parameters
    ----------
    datasets: list of xarray.Datasets representing range_angle for our workflow.  Can be any dataset object though.  We
              are just storing attributes in the range_angle one so far.

    Returns
    -------
    finaldict: dict, contains all unique attributes across all dataset, will append unique prim/secondary serial
               numbers and ignore duplicate settings entries
    """
    finaldict = {}

    buffered_settings = ''
    buffered_runtime_settings = ''

    fnames = []
    survey_nums = []
    cast_dump = {}

    if type(datasets) != list:
        datasets = [datasets]

    try:
        all_attrs = [datasets[x].attrs for x in range(len(datasets))]
    except AttributeError:
        all_attrs = datasets

    for d in all_attrs:
        for k, v in d.items():
            # settings gets special treatment for a few reasons...
            if k[0:7] == 'install':
                vals = json.loads(v)  # stored as a json string for serialization reasons
                try:
                    fname = vals.pop('raw_file_name')
                    if fname not in fnames:
                        # keep .all file names for their own attribute
                        fnames.append(fname)
                except KeyError:  # key exists in .all file but not in .kmall
                    pass
                    # print('{}: Unable to find "raw_file_name" key'.format(k))
                try:
                    sname = vals.pop('survey_identifier')
                    if sname not in survey_nums:
                        # keep survey identifiers for their own attribute
                        survey_nums.append(sname)
                except KeyError:  # key exists in .all file but not in .kmall
                    pass
                    # print('{}: Unable to find "raw_file_name" key'.format(k))
                vals = json.dumps(vals)

                # This is for the duplicate entries, just ignore these
                if vals == buffered_settings:
                    pass
                # this is for the first settings entry
                elif not buffered_settings:
                    buffered_settings = vals
                    finaldict[k] = vals
                # all unique entries after the first are saved
                else:
                    finaldict[k] = vals
            elif k[0:7] == 'runtime':
                vals = json.loads(v)  # stored as a json string for serialization reasons
                # we pop out these three keys because they are unique across all runtime params.  You end up with like
                # fourty records, all with only them being unique.  Not useful.  Rather only store important differences.
                try:
                    counter = vals.pop('Counter')
                except KeyError:  # key exists in .all file but not in .kmall
                    counter = ''
                    # print('{}: Unable to find "raw_file_name" key'.format(k))
                try:
                    mindepth = vals.pop('MinDepth')
                except KeyError:  # key exists in .all file but not in .kmall
                    mindepth = ''
                    # print('{}: Unable to find "MinDepth" key'.format(k))
                try:
                    maxdepth = vals.pop('MaxDepth')
                except KeyError:  # key exists in .all file but not in .kmall
                    maxdepth = ''
                    # print('{}: Unable to find "MaxDepth" key'.format(k))
                vals = json.dumps(vals)

                # This is for the duplicate entries, just ignore these
                if vals == buffered_runtime_settings:
                    pass
                # this is for the first settings entry
                elif not buffered_runtime_settings:
                    buffered_runtime_settings = vals
                    vals = json.loads(v)
                    vals['Counter'] = counter
                    finaldict[k] = json.dumps(vals)
                # all unique entries after the first are saved
                else:
                    vals = json.loads(v)
                    vals['Counter'] = counter
                    finaldict[k] = json.dumps(vals)

            # save all unique serial numbers
            elif k in ['system_serial_number', 'secondary_system_serial_number'] and k in list(finaldict.keys()):
                if finaldict[k] != v:
                    finaldict[k] = np.array(finaldict[k])
                    finaldict[k] = np.append(finaldict[k], v)
            # save all casts, use this to only pull the first unique cast later (casts are being saved in each line
            #   with a time stamp of when they appear in the data.  Earliest time represents the closest to the actual
            #   cast time).
            elif k[0:7] == 'profile':
                cast_dump[k] = v
            elif k[0:3] == 'min':
                if k in finaldict:
                    finaldict[k] = np.min([v, finaldict[k]])
                else:
                    finaldict[k] = v
            elif k[0:3] == 'max':
                if k in finaldict:
                    finaldict[k] = np.max([v, finaldict[k]])
                else:
                    finaldict[k] = v
            elif k not in finaldict:
                finaldict[k] = v

    if fnames:
        finaldict['system_serial_number'] = finaldict['system_serial_number'].tolist()
        finaldict['secondary_system_serial_number'] = finaldict['secondary_system_serial_number'].tolist()
        finaldict['multibeam_files'] = list(np.unique(sorted(fnames)))
    if survey_nums:
        finaldict['survey_number'] = list(np.unique(survey_nums))
    if cast_dump:
        sorted_kys = sorted(cast_dump)
        unique_casts = []
        for k in sorted_kys:
            if cast_dump[k] not in unique_casts:
                unique_casts.append(cast_dump[k])
                finaldict[k] = cast_dump[k]
    return finaldict


def divide_arrays_by_time_index(arrs, idx):
    """
    Simple method for indexing a list of arrays

    Parameters
    ----------
    arrs: list of xarray DataArray or Dataset objects
    idx: numpy array index

    Returns
    -------
    list of indexed xarray DataArray or Dataset objects

    """
    dat = []
    for ar in arrs:
        dat.append(ar[idx])
    return dat


def combine_arrays_to_dataset(arrs, arrnames):
    """
    Build a dataset from a list of Xarray DataArrays, given a list of names for each array.

    Parameters
    ----------
    arrs: list, xarray DataArrays you want in your xarray Dataset
    arrnames: list, string name identifiers for each array, will be the variable name in the Dataset

    Returns
    -------
    xarray Dataset with variables equal to the provided arrays

    """
    if len(arrs) != len(arrnames):
        raise ValueError('Please provide an equal number of names to dataarrays')
    dat = {a: arrs[arrnames.index(a)] for a in arrnames}
    dset = xr.Dataset(dat)
    return dset


def validate_merge(xarr, rootgroup):
    """
    Merge is used when writing a new variable/array to an existing zarr datastore.  We need to ensure that the merge
    is actually going to work before we execute.  Function checks if there is an existing time index that
    matches the array.

    Parameters
    ----------
    xarr: xarray Dataset object
    rootgroup: zarr datastore

    Returns
    -------
    bool, True if merge process is good to go

    """
    if ('time' in xarr) and ('time' in rootgroup):
        if xarr['time'][0] in rootgroup['time']:
            pass
        else:
            print('Merge failed: Expected provided time to exist in zarr datastore')
            return False
    return True


def my_xarr_add_attribute(attrs, outputpth, sync):
    """
    Add the provided attrs dict to the existing attribution of the zarr instance at outputpth

    Parameters
    ----------
    attrs: dict, dictionary of combined attributes from xarray datasets, None if no attributes exist
    outputpth: str, path to zarr group to either be created or append to
    sync: DaskProcessSynchronizer, dask distributed lock for parallel read/writes

    Returns
    -------
    outputpth: pth, path to the final zarr group

    """
    # mode 'a' means read/write, create if doesnt exist
    rootgroup = zarr.open(outputpth, mode='a', synchronizer=sync)
    _my_xarr_to_zarr_writeattributes(rootgroup, attrs)
    return outputpth


def my_xarr_to_zarr(xarr, attrs, outputpth, sync, dataloc, append_dim='time', finalsize=None, merge=False,
                    override_chunk_size=None):
    """
    I've been unable to get the zarr append/write functionality to work with the dask distributed cluster.  Even when
    using dask's own distributed lock, I've found that the processes are stepping on each other and each array is not
    written in it's entirety.

    The solution I have here ignores the zarr append method and manually specifies indexes for each worker to write to
    (dataloc).  I believe with this method, you probably don't even need the sync, but I leave it in here in case it
    comes into play when writing attributes/metadata.

    Appending to existing zarr is a problem.  I don't yet know of a way to append to a written chunk.  For instance,
    if the chunksize you want is 5000 and you write an array of 2000 values, your actual logged chunksize is 2000.  If
    you append to this, you now have to append with a chunksize of 2000, I don't know how to fill in existing chunks to
    get chunksize-length chunks.

    Parameters
    ----------
    xarr: xarray Dataset, one chunk of the final range_angle/attitude/navigation xarray Dataset we are writing
    attrs: dict, dictionary of combined attributes from xarray datasets, None if no attributes exist
    outputpth: str, path to zarr group to either be created or append to
    sync: DaskProcessSynchronizer, dask distributed lock for parallel read/writes
    dataloc: list, list of start/end time indexes, ex: [100,600] for writing to the 100th time to the 600th time in
                   this chunk
    append_dim: str, default is 'time', alter this to change the dimension name you want to append to
    finalsize: int, if given, resize the zarr group time dimension to this value, if None then do no resizing
    merge: bool, if True, this is an operation to merge an existing zarr store with a new xarray DataSet that has
                   equal dimensions, or is a slice of a DataSet that has equal dimensions
    override_chunk_size: optional, int, if provided will use this as the chunksize of the zarr dataset

    Returns
    -------
    outputpth: pth, path to the final zarr group

    """
    dims_of_arrays = _my_xarr_to_zarr_build_arraydimensions(xarr)

    # mode 'a' means read/write, create if doesnt exist
    rootgroup = zarr.open(outputpth, mode='a', synchronizer=sync)
    # merge is for merging a new dataset with an existing zarr datastore.  Expect the same time dimension, just new
    #    variables
    if merge:
        if not validate_merge(xarr, rootgroup):
            return

    existing_arrs = [t for t in rootgroup.array_keys()]

    _my_xarr_to_zarr_writeattributes(rootgroup, attrs)

    # var here will represent one of the array names, 'beampointingangle', 'time', 'soundspeed', etc.
    for var in dims_of_arrays:
        if merge and var in ['time', 'beam', 'sector']:
            continue
        if override_chunk_size is not None:
            chunksize = override_chunk_size
        else:
            chunksize = dims_of_arrays[var][1]

        data_loc_copy = dataloc.copy()
        # these do not need appending, just ensure they maintain uniques
        if var in ['sector', 'beam', 'xyz']:
            if var in existing_arrs:
                if not np.array_equal(xarr[var].values, rootgroup[var]):
                    raise ValueError(
                        'Found inconsistent ' + var + ' dimension: ' + xarr[var].values + ' and ' + rootgroup[var])
            else:
                rootgroup[var] = xarr[var].values
        else:
            # want to get the length of the time dimension, so you know which dim to append to
            timlength = len(xarr[var][append_dim])
            timaxis = xarr[var].shape.index(timlength)
            # shape is extended on append.  chunks will always be equal to shape, as each run of this function will be
            #     done on one chunk of data by one worker

            if var in existing_arrs:
                # array to be appended
                newarr = zarr.array(xarr[var].values, shape=dims_of_arrays[var][1], chunks=chunksize)

                # the last write will often be less than the block size.  This is allowed in the zarr store, but we
                #    need to correct the index for it.
                if timlength != data_loc_copy[1] - data_loc_copy[0]:
                    data_loc_copy[1] = data_loc_copy[0] + timlength

                # location for new data, assume constant chunksize (as we are doing this outside of this function)
                chunk_time_range = slice(data_loc_copy[0], data_loc_copy[1])
                chunk_idx = tuple(
                    chunk_time_range if dims_of_arrays[var][1].index(i) == timaxis else slice(0, i) for i in
                    dims_of_arrays[var][1])
                # if finalsize:  # im about this far with appending to existing zarr datastores, dealing with chunksizes is not a simple matter
                #     finalshp = tuple(
                #         finalsize if dims_of_arrays[var][1].index(x) == timaxis else x for x in dims_of_arrays[var][1])
                #     rootgroup[var].resize(finalshp)
                rootgroup[var][chunk_idx] = newarr
            else:
                startingshp = tuple(
                    finalsize if dims_of_arrays[var][1].index(x) == timaxis else x for x in dims_of_arrays[var][1])

                newarr = rootgroup.create_dataset(var, shape=dims_of_arrays[var][1], chunks=chunksize,
                                                  dtype=xarr[var].dtype, synchronizer=sync, fill_value=None)
                newarr[:] = xarr[var].values
                newarr.resize(startingshp)

        # print('Zarr write: {}, {}, {}'.format(outputpth, var, dims_of_arrays[var][1]))
        # _ARRAY_DIMENSIONS is used by xarray for connecting dimensions with zarr arrays
        rootgroup[var].attrs['_ARRAY_DIMENSIONS'] = dims_of_arrays[var][0]
    return outputpth


def get_new_chunk_locations_zarr(outputpth, data_locs, ideal_chunk_size):
    """
    Data locs as created is naive to the existing rootgroup data.  We want data_locs to represent the index of the
    rootgroup where the new data should be written.  So here we take the time dim of the rootgroup and adjust the
    data_locs accordingly.

    If the data locs don't correspond with this chunk size, we have a problem, as there isn't any way to rechunk zarr
    arrays.

    Parameters
    ----------
    outputpth: str, path to the zarr rootgroup
    data_locs: list of lists, [start time index, end time index] for xarr, ex: [0,1000] if xarr time dimension is 1000
               long.
    ideal_chunk_size: int, chunk size for the array.

    Returns
    -------
    data_locs: either the original data_locs if this is a new zarr rootgroup, or an adjusted data_locs for the existing
               rootgroup data.

    """
    if os.path.exists(outputpth):
        rootgroup = zarr.open(outputpth, mode='r')  # only opens if the path exists
        time_arr = rootgroup.time  # we assume that we are appending to the time dim
        arr_size = time_arr.shape[0]

        first_chunk = data_locs[0]
        if np.diff(first_chunk)[0] != ideal_chunk_size and len(data_locs) > 1:
            # if this is the case, we would have to adjust all the data_locs to the actual zarr chunk size
            raise NotImplementedError('get_new_chunk_locations_zarr: rechunking data locations not currently supported.')
        new_data_locs = [[dl[0] + arr_size, dl[1] + arr_size] for dl in data_locs]
        return new_data_locs
    else:
        return data_locs


def _interp_across_chunks_xarrayinterp(xarr, dimname, chnk_time):
    """
    Runs xarr interp on an individual chunk, extrapolating to cover boundary case

    Parameters
    ----------
    xarr: xarray DataArray or Dataset, object to be interpolated
    dimname: str, dimension name to interpolate
    chnk_time: xarray DataArray, time to interpolate to

    Returns
    -------
    Interpolated xarr object.

    """
    if dimname == 'time':
        try:  # dataarray workflow, use 'values' to access the numpy array
            chnk_time = chnk_time.values
        except AttributeError:
            pass
        return xarr.interp(time=chnk_time, method='linear', assume_sorted=True, kwargs={'bounds_error': True,
                                                                                        'fill_value': 'extrapolate'})
    else:
        raise NotImplementedError('Only "time" currently supported dim name')


def _interp_across_chunks_construct_times(xarr, new_times, dimname):
    """
    Takes in the existing xarray dataarray/dataset (xarr) and returns chunk indexes and times that allow for
    interpolating to the desired xarray dataarray/dataset (given as new_times).  This allows us to interp across
    the dask array chunks without worrying about boundary cases between worker blocks.

    Parameters
    ----------
    xarr: xarray DataArray or Dataset, object to be interpolated
    new_times: xarray DataArray, times for the array to be interpolated to
    dimname: str, dimension name to interpolate

    Returns
    -------
    chnk_idxs: list of lists, each element is a list containing time indexes for the chunk, ex: [[0,2000], [2000,4000]]
    chnkwise_times: list or DataArray, each element is the section of new_times that applies to that chunk

    """
    # first go ahead and chunk the array if chunks do not exist
    if not xarr.chunks:
        xarr = xarr.chunk()

    try:
        xarr_chunks = xarr.chunks[0]  # works for xarray DataArray
    except KeyError:
        xarr_chunks = xarr.chunks[dimname]  # works for xarray Dataset

    chnk_end = np.cumsum(np.array(xarr_chunks)) - 1
    chnk_end_time = xarr[dimname][chnk_end].values

    #  this is to ensure that we cover the desired time, extrapolate to cover the min/max desired time
    #  - when we break up the times to interp to (new_times) we want to ensure the last chunk covers all the end times
    chnk_end_time[-1] = new_times[-1] + 1
    try:
        # have to compute here, searchsorted not supported for dask arrays, but it is so much faster (should be sorted)
        endtime_idx = np.searchsorted(new_times.compute(), chnk_end_time)
    except AttributeError:
        # new_times is a numpy array, does not need compute
        endtime_idx = np.searchsorted(new_times, chnk_end_time)

    chnkwise_times = np.split(new_times, endtime_idx)[:-1]  # drop the last, its empty

    # build out the slices
    # add one to get the next entry for each chunk
    slices_endtime_idx = np.insert(chnk_end + 1, 0, 0)
    chnk_idxs = [[slices_endtime_idx[i], slices_endtime_idx[i+1]] for i in range(len(slices_endtime_idx)-1)]

    # only return chunk blocks that have valid times in them
    empty_chunks = np.array([chnkwise_times.index(i) for i in chnkwise_times if i.size == 0])
    for idx in empty_chunks[::-1]:  # go backwards to preserve index in list as we remove elements
        del chnk_idxs[idx]
        del chnkwise_times[idx]
    return chnk_idxs, chnkwise_times


def slice_xarray_by_dim(arr, dimname='time', start_time=None, end_time=None):
    """
    Slice the input xarray dataset/dataarray by provided start_time and end_time. Start/end time do not have to be
    values in the dataarray index to be used, this function will find the nearest times.

    If times provided are outside the array, will return the original array.

    If times are not provided, will return the original array

    Parameters
    ----------
    arr: xarray Dataarray/Dataset with an index of dimname
    dimname: str, name of dimension to use with selection/slicing
    start_time: float, start time of slice
    end_time: float, end time of slice

    Returns
    -------
    xarray dataarray/dataset sliced to the input start time and end time

    """
    if start_time is None and end_time is None:
        return arr

    if start_time is not None:
        nearest_start = float(arr[dimname].sel(time=start_time, method='nearest'))
    else:
        nearest_start = float(arr[dimname][0])

    if end_time is not None:
        nearest_end = float(arr[dimname].sel(time=end_time, method='nearest'))
    else:
        nearest_end = float(arr[dimname][-1])

    if start_time is not None and end_time is not None:
        if nearest_end == nearest_start:
            # if this is true, you have start/end times that are outside the scope of the data.  The start/end times will
            #  be equal to either the start of the dataset or the end of the dataset, depending on when they fall
            return None
    rnav = arr.sel(time=slice(nearest_start, nearest_end))
    rnav = rnav.chunk(rnav.sizes)  # do this to get past the unify chunks issue, since you are slicing here, you end up with chunks of different sizes
    return rnav


def interp_across_chunks(xarr, new_times, dimname='time', daskclient=None):
    """
    Takes in xarr and interpolates to new_times.  Ideally we could use xarray interp_like or interp, but neither
    of these are implemented with support for chunked dask arrays.  Therefore, we have to determine the times of
    each chunk and interpolate individually.  To allow for the case where a value is between chunks or right on
    the boundary, we extend the chunk time to buffer the gap.

    Parameters
    ----------
    xarr: xarray DataArray or Dataset, object to be interpolated
    new_times: xarray DataArray, times for the array to be interpolated to
    dimname: str, dimension name to interpolate
    daskclient: dask.distributed.client or None, if running outside of dask cluster

    Returns
    -------
    newarr: xarray DataArray or Dataset, interpolated xarr

    """
    if type(xarr) not in [xr.DataArray, xr.Dataset]:
        raise NotImplementedError('Only xarray DataArray and Dataset objects allowed.')
    if len(list(xarr.dims)) > 1:
        raise NotImplementedError('Only one dimensional data is currently supported.')

    # with heading you have to deal with zero crossing, occassionaly see lines where you end up interpolating heading
    #  from 0 to 360, which gets you something around 180deg.  Take the 360 complement and interp that, return it back
    #  to 0-360 domain after
    needs_reverting = False
    if type(xarr) == xr.DataArray:
        if xarr.name == 'heading':
            needs_reverting = True
            xarr = xr.DataArray(np.float32(np.rad2deg(np.unwrap(np.deg2rad(xarr)))), coords=[xarr.time], dims=['time'])
    else:
        if 'heading' in list(xarr.data_vars.keys()):
            needs_reverting = True
            xarr['heading'] = xr.DataArray(np.float32(np.rad2deg(np.unwrap(np.deg2rad(xarr.heading)))), coords=[xarr.time],
                                           dims=['time'])

    chnk_idxs, chnkwise_times = _interp_across_chunks_construct_times(xarr, new_times, dimname)
    xarrs_chunked = [xarr.isel({dimname: slice(i, j)}).chunk(j-i,) for i, j in chnk_idxs]
    if daskclient is None:
        interp_arrs = []
        for ct, xar in enumerate(xarrs_chunked):
            interp_arrs.append(_interp_across_chunks_xarrayinterp(xar, dimname, chnkwise_times[ct]))
        newarr = xr.concat(interp_arrs, dimname)
    else:
        interp_futs = daskclient.map(_interp_across_chunks_xarrayinterp, xarrs_chunked, [dimname] * len(chnkwise_times),
                                     chnkwise_times)
        newarr = daskclient.submit(xr.concat, interp_futs, dimname).result()

    if needs_reverting and type(xarr) == xr.DataArray:
        newarr = newarr % 360
    elif needs_reverting and type(xarr) == xr.Dataset:
        newarr['heading'] = newarr['heading'] % 360

    assert(len(new_times) == len(newarr[dimname])), 'interp_across_chunks: Input/Output shape is not equal'
    return newarr


def clear_data_vars_from_dataset(dataset, datavars):
    """
    Some code to handle dropping data variables from xarray Datasets in different containers.  We use lists of Datasets,
    dicts of Datasets and individual Datasets in different places.  Here we can just pass in whatever, drop the
    variable or list of variables, and get the Dataset back.

    Parameters
    ----------
    dataset: xarray Dataset, list, or dict of xarray Datasets
    datavars: str or list, variables we wish to drop from the xarray Dataset

    Returns
    -------
    dataset: original Dataset with dropped variables

    """
    if type(datavars) == str:
        datavars = [datavars]

    for datavar in datavars:
        if type(dataset) == dict:  # I frequently maintain a dict of datasets for each sector
            for sec_ident in dataset:
                if datavar in dataset[sec_ident].data_vars:
                    dataset[sec_ident] = dataset[sec_ident].drop_vars(datavar)
        elif type(dataset) == list:  # here if you have lists of Datasets
            for cnt, dset in enumerate(dataset):
                if datavar in dset.data_vars:
                    dataset[cnt] = dataset[cnt].drop_vars(datavar)
        elif type(dataset) == xr.Dataset:
            if datavar in dataset.data_vars:
                dataset = dataset.drop_vars(datavar)
    return dataset


def stack_nan_array(dataarray, stack_dims=('time', 'beam')):
    """
    To handle NaN values in our input arrays, we flatten and index only the valid values.  This comes into play with
    beamwise arrays that have NaN where there were no beams.

    See reform_nan_array to rebuild the originaly array

    Parameters
    ----------
    dataarray: xarray DataArray, array that we need to flatten and index non-NaN values
    stack_dims: tuple, dims of our input data

    Returns
    -------
    orig_idx: numpy array, indexes of the original data
    dataarray_stck: xarray DataArray, multiindexed and flattened

    """
    orig_idx = np.where(~np.isnan(dataarray))
    dataarray_stck = dataarray.stack(stck=stack_dims)
    nan_idx = ~np.isnan(dataarray_stck).compute()
    dataarray_stck = dataarray_stck[nan_idx]
    return orig_idx, dataarray_stck


def flatten_bool_xarray(datarray: xr.DataArray, cond: xr.DataArray, retain_dim: str = 'time', drop_var: str = None):
    """
    Takes in a two dimensional DataArray with core dimension 'time' and a second dimension that has only one valid
    value according to provided cond.  Outputs DataArray with those values and either only the 'time' dimension
    (drop_var is the dimension name to drop) or with both dimensions intact.

    tst.raw_ping.tiltangle
    Out[11]:
    <xarray.DataArray 'tiltangle' (time: 7836, sector: 16)>
    dask.array<zarr, shape=(7836, 16), dtype=float64, chunksize=(5000, 16), chunktype=numpy.ndarray>
    Coordinates:
      * sector   (sector) <U12 '218_0_070000' '218_0_071000' ... '218_2_090000'
      * time     (time) float64 1.474e+09 1.474e+09 ... 1.474e+09 1.474e+09

    tst.raw_ping.tiltangle.isel(time=0).values
    Out[6]:
    array([ 0.  ,  0.  ,  0.  ,  0.  ,  0.  ,  0.  ,  0.  , -0.38,  0.  ,
            0.  ,  0.  ,  0.  ,  0.  ,  0.  ,  0.  ,  0.  ])

    answer = fqpr_generation.flatten_bool_xarray(tst.raw_ping.tiltangle, tst.raw_ping.ntx > 0,
                                                 drop_var='sector')
    answer
    Out[10]:
    <xarray.DataArray 'tiltangle' (time: 7836)>
    dask.array<vindex-merge, shape=(7836,), dtype=float64, chunksize=(7836,), chunktype=numpy.ndarray>
    Coordinates:
        * time     (time) float64 1.474e+09 1.474e+09 ... 1.474e+09 1.474e+09

    answer.isel(time=0).values
    Out[9]: array(-0.38)

    Parameters
    ----------
    datarray
        2 dimensional xarray DataArray
    cond
        boolean mask for datarray, see example above
    retain_dim
        core dimension of datarray
    drop_var
        dimension to drop, optional

    Returns
    -------

    """
    datarray = datarray.where(cond)
    if datarray.ndim == 2:
        # True/False where non-NaN values are
        true_idx = np.argmax(datarray.notnull().values, axis=1)
        data_idx = xr.DataArray(true_idx, coords={retain_dim: datarray.time}, dims=[retain_dim])
    else:
        raise ValueError('Only 2 dimensional DataArray objects are supported')

    answer = datarray[0:len(datarray.time), data_idx]
    if drop_var:
        answer = answer.drop_vars(drop_var)
    return answer


def reform_nan_array(dataarray_stack, orig_idx, orig_shape, orig_coords, orig_dims):
    """
    To handle NaN values in our input arrays, we flatten and index only the valid values.  Here we rebuild the
    original square shaped arrays we need using one of the original arrays as reference.

    See stack_nan_array

    Parameters
    ----------
    dataarray_stack: xarray DataArray, flattened array that we just interpolated
    orig_idx: tuple, 2 elements, one for 1st dimension indexes and one for 2nd dimension indexes, see np.where
    orig_shape: tuple, original shape
    orig_coords: xarray DataArrayCoordinates, original coords
    orig_dims: tuple, original dims

    Returns
    -------
    final_arr: xarray DataArray, values of arr, filled to be square with NaN values, coordinates of ref_array

    """
    final_arr = np.empty(orig_shape, dtype=dataarray_stack.dtype)
    final_arr[:] = np.nan
    final_arr[orig_idx] = dataarray_stack
    final_arr = xr.DataArray(final_arr, coords=orig_coords, dims=orig_dims)
    return final_arr


def reload_zarr_records(pth, skip_dask=False):
    """
    After writing new data to the zarr data store, you need to refresh the xarray Dataset object so that it
    sees the changes.  We do that here by just re-running open_zarr.

    Returns
    -------
    pth: string, path to xarray Dataset stored as zarr datastore
    skip_dask: bool, if True, skip the dask process synchronizer as you are not running dask distributed

    """
    if os.path.exists(pth):
        if not skip_dask:
            return xr.open_zarr(pth, synchronizer=DaskProcessSynchronizer(pth),
                                mask_and_scale=False, decode_coords=False, decode_times=False,
                                decode_cf=False, concat_characters=False)
        else:
            return xr.open_zarr(pth, synchronizer=None,
                                mask_and_scale=False, decode_coords=False, decode_times=False,
                                decode_cf=False, concat_characters=False)
    else:
        print('Unable to reload, no paths found: {}'.format(pth))
        return None


def return_chunk_slices(xarr):
    """
    Xarray objects are chunked for easy parallelism.  When we write to zarr stores, chunks become segregated, so when
    operating on xarray objects, it makes sense to do it one chunk at a time sometimes.  Here we return slices so that
    we can only pull one chunk into memory at a time.

    EX:
    xarr
    Out[64]:
    <xarray.Dataset>
    Dimensions:  (time: 245236)
    Coordinates:
       * time     (time) float64 1.583e+09 1.583e+09 ... 1.583e+09 1.583e+09
    Data variables:
       heading  (time) float32 dask.array<chunksize=(20000,), meta=np.ndarray>
        heave    (time) float32 dask.array<chunksize=(20000,), meta=np.ndarray>
        pitch    (time) float32 dask.array<chunksize=(20000,), meta=np.ndarray>
       roll     (time) float32 dask.array<chunksize=(20000,), meta=np.ndarray>

    xarr.chunks
    Out[67]: Frozen(SortedKeysDict({'time': (20000, 20000, 20000, 20000, 20000, 20000, 20000, 20000, 20000, 20000,
                                             20000, 20000, 5236)}))

    return_chunk_slices(xarr)
    Out[66]:
    [slice(0, 20000, None),
     slice(20000, 40000, None),
     slice(40000, 60000, None),
     slice(60000, 80000, None),
     slice(80000, 100000, None),
     slice(100000, 120000, None),
     slice(120000, 140000, None),
     slice(140000, 160000, None),
     slice(160000, 180000, None),
     slice(180000, 200000, None),
     slice(200000, 220000, None),
     slice(220000, 240000, None),
     slice(240000, 245236, None)]

    Parameters
    ----------
    xarr: xarray Dataset/DataArray object, must be only one dimension currently

    Returns
    -------
    list of slices for the indices of each chunk

    """

    try:
        chunk_dim = list(xarr.chunks.keys())
        if len(chunk_dim) > 1:
            raise NotImplementedError('Only 1 dimensional xarray objects supported at this time')
            return None
    except AttributeError:
        print('Only xarray objects are supported')
        return None

    chunk_dim = chunk_dim[0]
    chunks = list(xarr.chunks.values())[0]
    chunk_size = chunks[0]
    chunk_slices = [slice(i * chunk_size, i * chunk_size + chunk_size) for i in range(len(chunks))]

    # have to correct last slice, as last chunk is equal to the length of the array modulo chunk size
    total_len = xarr.dims[chunk_dim]
    last_chunk_size = xarr.dims[chunk_dim] % chunk_size
    if last_chunk_size:
        chunk_slices[-1] = slice(total_len - last_chunk_size, total_len)
    else:  # last slice fits perfectly, they have no remainder some how
        pass

    return chunk_slices


def _find_gaps_split(datagap_times, existing_gap_times):
    """
    helper for compare_and_find_gaps.  A function to use in a loop to continue splitting gaps until they no longer
    include any existing gaps

    datagap_times = [[0,5], [30,40], [70, 82], [90,100]]
    existing_gap_times = [[10,15], [35,45], [75,80], [85,95]]

    split_dgtime = [[0, 5], [30, 40], [70, 75], [80, 82], [90, 100]]

    Parameters
    ----------
    datagap_times: list, list of two element lists (start time, end time) for the gaps found in the new data
    existing_gap_times: list, list of two element lists (start time, end time) for the gaps found in the existing data

    Returns
    -------
    split_dgtime: list, list of two element lists (start time, end time) for the new data gaps split around the existing data gaps

    """
    split = False
    split_dgtime = []
    for dgtime in datagap_times:
        for existtime in existing_gap_times:
            # datagap contains an existing gap, have to split the datagap
            if (dgtime[0] <= existtime[0] <= dgtime[1]) and (dgtime[0] <= existtime[1] <= dgtime[1]):
                split_dgtime.append([dgtime[0], existtime[0]])
                split_dgtime.append([existtime[1], dgtime[1]])
                split = True
                break
        if not split:
            split_dgtime.append(dgtime)
        else:
            split = False
    return split_dgtime


def compare_and_find_gaps(source_dat, new_dat, max_gap_length=1.0, dimname='time'):
    """
    So far, mostly used with sbets.  Converted SBET would be the new_dat and the existing navigation in Kluster would
    be the source_dat.  You'd be interested to know if there were gaps in the sbet greater than a certain length that
    did not coincide with existing gaps related to stopping/starting logging or something.  Here we find gaps in the
    new_dat of size greater than max_gap_length and trim them to the gaps found in source_dat

    Parameters
    ----------
    source_dat: xarray DataArray/Dataset, object with dimname as coord that you want to use as the basis for comparison
    new_dat: xarray DataArray/Dataset that you want to find the gaps in
    max_gap_length: float, maximum acceptable gap
    dimname: str, name of the dimension you want to find the gaps in

    Returns
    -------
    finalgaps: numpy array, nx2 where n is the number of gaps found

    """
    # gaps in source, if a gap in the new data is within a gap in the source, it is not a gap
    existing_gaps = np.argwhere(source_dat[dimname].diff(dimname).values > max_gap_length)
    existing_gap_times = [[float(source_dat[dimname][gp]), float(source_dat[dimname][gp + 1])] for gp in existing_gaps]

    # look for gaps in the new data
    datagaps = np.argwhere(new_dat[dimname].diff(dimname).values > max_gap_length)
    datagap_times = [[float(new_dat[dimname][gp]), float(new_dat[dimname][gp + 1])] for gp in datagaps]

    # consider postprocessed nav starting too late or ending too early as a gap as well
    if new_dat[dimname].min() > source_dat[dimname].time.min() + max_gap_length:
        datagap_times.insert([float(source_dat[dimname].time.min()), float(new_dat[dimname].min())])
    if new_dat[dimname].max() + max_gap_length < source_dat[dimname].time.max():
        datagap_times.append([float(new_dat[dimname].max()), float(source_dat[dimname].time.max())])

    # first, split all the gaps if they contain existing time gaps, keep going until you no longer find contained gaps
    splitting = True
    while splitting:
        dg_split_gaps = _find_gaps_split(datagap_times, existing_gap_times)
        if dg_split_gaps != datagap_times:  # you split
            datagap_times = dg_split_gaps
        else:
            splitting = False

    # next adjust gap boundaries if they overlap with existing gaps
    finalgaps = []
    for dgtime in datagap_times:
        for existtime in existing_gap_times:
            # datagap is fully within an existing gap in the source data, just dont include it
            if (existtime[0] <= dgtime[0] <= existtime[1]) and (existtime[0] <= dgtime[1] <= existtime[1]):
                continue
            # partially covered
            if existtime[0] < dgtime[0] < existtime[1]:
                dgtime[0] = existtime[1]
            elif existtime[0] < dgtime[1] < existtime[1]:
                dgtime[1] = existtime[0]
        finalgaps.append(dgtime)

    return np.array(finalgaps)
