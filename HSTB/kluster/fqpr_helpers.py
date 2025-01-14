import os
from typing import Union
from pyproj import CRS
from pyproj.exceptions import CRSError
from HSTB.kluster import kluster_variables


def build_crs(zone_num: str = None, datum: str = None, epsg: str = None, projected: bool = True):
    horizontal_crs = None
    if epsg:
        try:
            horizontal_crs = CRS.from_epsg(int(epsg))
        except CRSError:  # if the CRS we generate here has no epsg, when we save it to disk we save the proj string
            horizontal_crs = CRS.from_string(epsg)
    elif not epsg and not projected:
        datum = datum.upper()
        if datum == 'NAD83':
            horizontal_crs = CRS.from_epsg(epsg_determinator('nad83(2011)'))
        elif datum == 'WGS84':
            horizontal_crs = CRS.from_epsg(epsg_determinator('wgs84'))
        else:
            err = '{} not supported.  Only supports WGS84 and NAD83'.format(datum)
            return horizontal_crs, err
    elif not epsg and projected:
        datum = datum.upper()
        zone = zone_num  # this will be the zone and hemi concatenated, '10N'
        try:
            zone, hemi = int(zone[:-1]), str(zone[-1:])
        except:
            raise ValueError(
                'construct_crs: found invalid projected zone/hemisphere identifier: {}, expected something like "10N"'.format(
                    zone))

        if datum == 'NAD83':
            horizontal_crs = CRS.from_epsg(epsg_determinator('nad83(2011)', zone=zone, hemisphere=hemi))
        elif datum == 'WGS84':
            horizontal_crs = CRS.from_epsg(epsg_determinator('wgs84', zone=zone, hemisphere=hemi))
        else:
            err = '{} not supported.  Only supports WGS84 and NAD83'.format(datum)
            return horizontal_crs, err
    return horizontal_crs, ''


def epsg_determinator(datum: str, zone: int = None, hemisphere: str = None):
    """
    Take in a datum identifer and optional zone/hemi for projected and return an epsg code

    Parameters
    ----------
    datum
        datum identifier string, one of nad83(2011), wgs84 supported for now
    zone
        integer utm zone number
    hemisphere
        hemisphere identifier, "n" for north, "s" for south

    Returns
    -------
    int
        epsg code
    """

    try:
        datum = datum.lower()
    except:
        raise ValueError('epsg_determinator: {} is not a valid datum string, expected "nad83(2011)" or "wgs84"')

    if zone is None and hemisphere is not None:
        raise ValueError('epsg_determinator: zone is required for projected epsg determination')
    if zone is not None and hemisphere is None:
        raise ValueError('epsg_determinator: hemisphere is required for projected epsg determination')
    if datum not in ['nad83(2011)', 'wgs84']:
        raise ValueError('epsg_determinator: {} not supported'.format(datum))

    if zone is None and hemisphere is None:
        if datum == 'nad83(2011)':  # using the 3d geodetic NAD83(2011)
            return kluster_variables.epsg_nad83
        elif datum == 'wgs84':  # using the 3d geodetic WGS84/ITRF2008
            return kluster_variables.epsg_wgs84
    else:
        hemisphere = hemisphere.lower()
        if datum == 'nad83(2011)':
            if hemisphere == 'n':
                if zone <= 19:
                    return 6329 + zone
                elif zone == 59:
                    return 6328
                elif zone == 60:
                    return 6329
        elif datum == 'wgs84':
            if hemisphere == 's':
                return 32700 + zone
            elif hemisphere == 'n':
                return 32600 + zone
    raise ValueError('epsg_determinator: no valid epsg for datum={} zone={} hemisphere={}'.format(datum, zone, hemisphere))


def return_files_from_path(pth: str, file_ext: tuple = ('.all',)):
    """
    Input files can be entered into an xarray_conversion.BatchRead instance as either a list, a path to a directory
    of multibeam files or as a path to a single file.  Here we return all the files in each of these scenarios as a list
    for the gui to display or to be analyzed in some other way

    Provide an optional fileext argument if you want to specify files with a different extension

    Parameters
    ----------
    pth
        either a list of files, a string path to a directory or a string path to a file
    file_ext
        file extension of the file(s) you are looking for

    Returns
    -------
    list
        list of files found
    """

    if type(pth) == list:
        if len(pth) == 1 and os.path.isdir(pth[0]):  # a list one element long that is a path to a directory
            return [os.path.join(pth[0], p) for p in os.listdir(pth[0]) if os.path.splitext(p)[1] in file_ext]
        else:
            return [p for p in pth if os.path.splitext(p)[1] in file_ext]
    elif os.path.isdir(pth):
        return [os.path.join(pth, p) for p in os.listdir(pth) if os.path.splitext(p)[1] in file_ext]
    elif os.path.isfile(pth):
        if os.path.splitext(pth)[1] in file_ext:
            return [pth]
        else:
            return []
    else:
        return []


def return_directory_from_data(data: Union[list, str]):
    """
    Given either a path to a zarr store, a path to a directory of multibeam files, a list of paths to multibeam files
    or a path to a single multibeam file, return the parent directory.

    Parameters
    ----------
    data
        a path to a zarr store, a path to a directory of multibeam files, a list of paths to multibeam files or a path
        to a single multibeam file.

    Returns
    -------
    output_directory: str, path to output directory
    """

    try:  # they provided a path to a zarr store or a path to a directory of .all files or a single .all file
        output_directory = os.path.dirname(data)
    except TypeError:  # they provided a list of files
        output_directory = os.path.dirname(data[0])
    return output_directory
