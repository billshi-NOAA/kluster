from HSTB.kluster.xarray_conversion import *
from HSTB.kluster.xarray_conversion import _xarr_is_bit_set, _build_serial_mask, _return_xarray_mintime, \
    _return_xarray_timelength, _divide_xarray_indicate_empty_future, _return_xarray_constant_blocks, \
    _merge_constant_blocks, _assess_need_for_split_correction, _correct_for_splits, _closest_prior_key_value, \
    _closest_key_value
from HSTB.kluster import kluster_variables


def test_xarr_is_bit_set():
    tst = xr.DataArray(np.arange(10))
    # 4 5 6 7 all have the third bit set
    ans = np.array([False, False, False, False, True, True, True, True, False, False])
    assert np.array_equal(_xarr_is_bit_set(tst, 3), ans)


def test_build_serial_mask():
    rec = {'ping': {'serial_num': np.array([40111, 40111, 40112, 40112, 40111, 40111])}}
    ids, msk = _build_serial_mask(rec)
    assert ids == ['40111', '40112']
    assert np.array_equal(msk[0], np.array([0, 1, 4, 5]))
    assert np.array_equal(msk[1], np.array([2, 3]))


def test_return_xarray_mintime():
    tstone = xr.DataArray(np.arange(10), coords={'time': np.arange(10)}, dims=['time'])
    tsttwo = xr.Dataset(data_vars={'tstone': tstone}, coords={'time': np.arange(10)})
    assert _return_xarray_mintime(tstone) == 0
    assert _return_xarray_mintime(tsttwo) == 0


def test_return_xarray_timelength():
    tstone = xr.DataArray(np.arange(10), coords={'time': np.arange(10)}, dims=['time'])
    tsttwo = xr.Dataset(data_vars={'tstone': tstone}, coords={'time': np.arange(10)})
    assert _return_xarray_timelength(tsttwo) == 10


def test_divide_xarray_indicate_empty_future():
    assert _divide_xarray_indicate_empty_future(None) is False
    assert _divide_xarray_indicate_empty_future(xr.Dataset({'tst': []}, coords={'time': []})) is False
    assert _divide_xarray_indicate_empty_future(xr.Dataset({'tst': [1, 2, 3]}, coords={'time': [1, 2, 3]})) is True


def test_return_xarray_constant_blocks():
    tstone = xr.DataArray(np.arange(100), coords={'time': np.arange(100)}, dims=['time'])
    tsttwo = xr.Dataset(data_vars={'tstone': tstone}, coords={'time': np.arange(100)})
    x1 = tsttwo.isel(time=slice(0, 33))
    x2 = tsttwo.isel(time=slice(33, 66))
    x3 = tsttwo.isel(time=slice(66, 100))
    xarrs = [x1, x2, x3]
    xlens = [33, 33, 34]
    chunks, totallen = _return_xarray_constant_blocks(xlens, xarrs, 10)

    assert totallen == 100
    assert chunks == [[[0, 10, x1]], [[10, 20, x1]], [[20, 30, x1]], [[30, 33, x1], [0, 7, x2]], [[7, 17, x2]],
                      [[17, 27, x2]], [[27, 33, x2], [0, 4, x3]], [[4, 14, x3]], [[14, 24, x3]], [[24, 34, x3]]]

    chunkdata = [x[2] for y in chunks for x in y]
    expected_data = [x1, x1, x1, x1, x2, x2, x2, x2, x3, x3, x3, x3]
    assert all([(c == chk).all() for c, chk in zip(chunkdata, expected_data)])


def test_merge_constant_blocks():
    tstone = xr.DataArray(np.arange(100), coords={'time': np.arange(100)}, dims=['time'])
    tsttwo = xr.Dataset(data_vars={'tstone': tstone}, coords={'time': np.arange(100)})
    newblocks = [[0, 3, tsttwo], [10, 13, tsttwo], [20, 23, tsttwo]]
    merged = _merge_constant_blocks(newblocks)
    assert np.array_equal(np.array([0, 1, 2, 10, 11, 12, 20, 21, 22]), merged.tstone.values)


def test_assess_need_for_split_correction():
    tstone = xr.DataArray(np.arange(100), coords={'time': np.arange(100)}, dims=['time'])
    tsttwo = xr.Dataset(data_vars={'tstone': tstone}, coords={'time': np.arange(100)})
    tstthree = xr.Dataset(data_vars={'tstone': tstone}, coords={'time': np.arange(99, 199)})
    assert _assess_need_for_split_correction(tsttwo, tstthree) is True

    tstfour = xr.Dataset(data_vars={'tstone': tstone}, coords={'time': np.arange(150, 250)})
    assert _assess_need_for_split_correction(tsttwo, tstfour) is False


def test_correct_for_splits():
    tstone = xr.DataArray(np.arange(100), coords={'time': np.arange(100)}, dims=['time'])
    tsttwo = xr.Dataset(data_vars={'tstone': tstone}, coords={'time': np.arange(100)})

    assert _correct_for_splits(tsttwo, True).tstone.values[0] == 1
    assert _correct_for_splits(tsttwo, False).tstone.values[0] == 0


def test_closest_prior_key_value():
    tstmps = [100.0, 1000.0, 10000.0, 100000.0, 1000000.0]
    key = 80584.3
    assert _closest_prior_key_value(tstmps, key) == 10000.0


def test_closest_key_value():
    tstmps = [100.0, 1000.0, 10000.0, 100000.0, 1000000.0]
    key = 80584.3
    assert _closest_key_value(tstmps, key) == 100000.0


def test_batch_read_configure_options():
    opts = batch_read_configure_options()
    expected_opts = {
        'ping': {'chunksize': kluster_variables.ping_chunk_size, 'chunks': kluster_variables.ping_chunks,
                 'combine_attributes': True, 'output_arrs': [], 'time_arrs': [], 'final_pths': None, 'final_attrs': None},
        'attitude': {'chunksize': kluster_variables.attitude_chunk_size, 'chunks': kluster_variables.att_chunks,
                     'combine_attributes': False, 'output_arrs': [], 'time_arrs': [], 'final_pths': None, 'final_attrs': None},
        'navigation': {'chunksize': kluster_variables.navigation_chunk_size, 'chunks': kluster_variables.nav_chunks,
                       'combine_attributes': False, 'output_arrs': [], 'time_arrs': [], 'final_pths': None, 'final_attrs': None}}
    assert opts == expected_opts
