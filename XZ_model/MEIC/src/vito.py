'''
INPUT:
    geo_em.d<domain>
    VITO input files

OUTPUT:
    wrfchemi_00z_d01: 0 ~ 11 h
    wrfchemi_12z_d01: 12 ~ 23 h

UPDATE:
    Xin Zhang:
       04/19/2020: Basic

Steps:
    1. Create WRF area by reading the info of geo* file
    2. Read MEIC nc files and map species to WRF-Chem species
        and assign to self.vito[species]
    3. Resample self.vito to WRF area, write attributes,
        and assign to self.chemi[species]
    4. Export self.chemi to replace variables two 12-hour netCDF files.

The VITO file just contains three species:
    NOx, PM25 and SO2.
We will replace these species in wrfchemi* files
    generated by mozcart.py which uses MEIC data.

'''

import logging
import os
from calendar import monthrange
from datetime import datetime, timedelta
from time import strftime

import numpy as np
import xarray as xr
from pyresample.bilinear import resample_bilinear
from pyresample.geometry import AreaDefinition, SwathDefinition
from pyresample.kd_tree import resample_custom, resample_nearest

# Choose the following line for info or debugging:
# logging.basicConfig(level=logging.INFO)
logging.basicConfig(level=logging.DEBUG)

# --- input --- #
data_path = '../input_files/'
output_dir = '../output_files/'
vito_filename = 'VITO_STD-RES-INVENTORY_EAST-CHINA.nc'
domain = 'd01'
resample_method = 'bilinear'  # nearest, bilinear or idw

# simulated date
# emissions of any day in the month are same
yyyy = 2019
mm = 7
dd = 25

# Please don't change the following paras
minhour = 0
maxhour = 23
delta = 1  # unit: hour
days = monthrange(yyyy, mm)[1]  # get number of days of the month
chem1 = output_dir+"wrfchemi_00z_d"+domain
chem2 = output_dir+"wrfchemi_12z_d"+domain

wrf_projs = {1: 'lcc',
             2: 'npstere',
             3: 'merc',
             6: 'eqc'
             }


class vito(object):
    def __init__(self, st, et, delta):
        self.get_info()
        self.read_vito()
        self.resample_WRF(st, et, delta)
        self.replace_var()

    def get_info(self, ):
        '''
        Read basic info from geo file generated by WPS
            If want to run this on your laptop and care about the space,
            you can use ncks to subset the nc file (we just need attrs)
            ncks -F -d Time,1,,1000 -v Times geo_em.d01.nc geo_em.d01_subset.nc
        ref: https://fabienmaussion.info/2018/01/06/wrf-projection/
        '''
        self.geo = xr.open_dataset(data_path + 'geo_em.'+domain+'.nc')
        attrs = self.geo.attrs
        i = attrs['i_parent_end']
        j = attrs['j_parent_end']

        # calculate attrs for area definition
        shape = (j, i)
        radius = (i*attrs['DX']/2, j*attrs['DY']/2)

        # create area as same as WRF
        area_id = 'wrf_circle'
        proj_dict = {'proj': wrf_projs[attrs['MAP_PROJ']],
                     'lat_0': attrs['MOAD_CEN_LAT'],
                     'lon_0': attrs['STAND_LON'],
                     'lat_1': attrs['TRUELAT1'],
                     'lat_2': attrs['TRUELAT2'],
                     'a': 6370000,
                     'b': 6370000}
        center = (0, 0)
        self.area_def = AreaDefinition.from_circle(area_id,
                                                   proj_dict,
                                                   center,
                                                   radius,
                                                   shape=shape)

    def read_vito(self, ):
        '''Read VITO data and convert to species in MOZART'''
        # read VITO nc file
        ds = xr.open_dataset(data_path+vito_filename)

        # molecular weights
        var_dict = {'E_NO': 14,
                    'E_SO2': 64,
                    # 'E_PM25': 1,
                    }

        # iterate variables
        types = ['Industry', 'Energy', 'Traffic', 'Residential', 'Fires']
        for name in var_dict.keys():
            logging.info(' '*8+'Map to '+name+' species')
            emi_exist = hasattr(self, 'emi')
            if name == 'E_NO':
                species = [name[2:]+'x_'+t for t in types]
            else:
                species = [name[2:]+'_'+t for t in types]
            ds_var = ds[species]

            if not emi_exist:
                # just read lon/lat once
                self.calc_area(ds_var)
                self.emi = self.get_ds(ds_var, name, var_dict, mm)

            else:
                self.emi[name] = self.get_ds(ds_var, name, var_dict, mm)[name]

            logging.debug(' '*8 +
                          ' min: ' + str(self.emi[name].min().values) +
                          ' max: ' + str(self.emi[name].max().values) +
                          ' mean ' + str(self.emi[name].mean().values)
                          )

    def calc_area(self, ds):
        '''Get the lon/lat and area (m2)of emission gridded data'''
        attrs = ds.attrs
        # get lon/lat bounds
        self.emi_lon_b = np.linspace(float(attrs['grid_westb']),
                                     float(attrs['grid_eastb']),
                                     ds.sizes['lon']+1)
        self.emi_lat_b = np.linspace(float(attrs['grid_northb']),
                                     float(attrs['grid_southb']),
                                     ds.sizes['lat']+1)

        # get lon/lat
        self.emi_lon = ds.coords['lon']
        self.emi_lat = ds.coords['lat']

        # ref: https://github.com/Timothy-W-Hilton/STEMPyTools
        lon_bounds2d, lat_bounds2d = np.meshgrid(self.emi_lon_b, self.emi_lat_b)
        EARTH_RADIUS = 6370000.0
        Rad_per_Deg = np.pi / 180.0

        ydim = lon_bounds2d.shape[0]-1
        xdim = lon_bounds2d.shape[1]-1
        area = np.full((ydim, xdim), np.nan)
        for j in range(ydim):
            for i in range(xdim):
                xlon1 = lon_bounds2d[j, i]
                xlon2 = lon_bounds2d[j, i+1]
                ylat1 = lat_bounds2d[j, i]
                ylat2 = lat_bounds2d[j+1, i]

                cap_ht = EARTH_RADIUS * (1 - np.sin(ylat1 * Rad_per_Deg))
                cap1_area = 2 * np.pi * EARTH_RADIUS * cap_ht
                cap_ht = EARTH_RADIUS * (1 - np.sin(ylat2 * Rad_per_Deg))
                cap2_area = 2 * np.pi * EARTH_RADIUS * cap_ht
                area[j, i] = abs(cap1_area - cap2_area) * abs(xlon1 - xlon2) / 360.0

        # save to DataArray
        self.emi_area = xr.DataArray(area,
                                     dims=['lat', 'lon'],
                                     coords={'lon': ds.coords['lon'],
                                             'lat': ds.coords['lat']}).rename('area')
        self.emi_area.attrs['units'] = 'm^2'

        # assign to ds
        lon2d, lat2d = np.meshgrid(ds.coords['lon'], ds.coords['lat'])
        ds['longitude'] = xr.DataArray(lon2d,
                                       coords=[ds.coords['lat'], ds.coords['lon']],
                                       dims=['lat', 'lon'])
        ds['latitude'] = xr.DataArray(lat2d,
                                      coords=[ds.coords['lat'], ds.coords['lon']],
                                      dims=['lat', 'lon'])


    def get_ds(self, ds, name, var_dict, mm):
        '''Generate the Dataset for species'''
        seconds = days*24*3600
        hours = days*24

        # because the fill value is same for all variables,
        # I just use industry data.
        if name == 'E_NO':
            varname = name.split('_')[-1]+'x_Industry'
        else:
            varname = name.split('_')[-1]+'_Industry'

        # subset data to selected month
        ds = ds.where(ds != ds[varname].attrs['MissingValue'])
        ds = ds.where(ds['time.month'] == mm, drop=True)

        if name == 'E_PM25':
            # WRF-Chem unit: ug/m3 m/s
            ds = ds*1e15/self.emi_area/(seconds*var_dict[name])

        elif name in ['E_NO', 'E_SO2']:
            # WRF-Chem unit: mol km-2 hr-1
            ds = ds*1e9/(self.emi_area/1e6)/(hours*var_dict[name])

        # read hourly factor table
        kind = ['Fires', 'Industry', 'Energy', 'Residential', 'Traffic']
        try:
            # shape: 24*5 (time*kind)
            table = xr.DataArray(np.genfromtxt('./hourly_factor.csv',
                                               delimiter=',',
                                               comments='#',
                                               usecols=(0, 1, 2, 3, 4),
                                               skip_header=2),
                                 dims=['hour', 'kind'],
                                 coords={'hour': np.arange(0, 24, 1),
                                         'kind': kind}
                                 )
            table = table/(table.sum(dim='hour')/24)

        except OSError:
            logging.info(' '*8 +
                         'hourly_factor.csv does not exist, use 1 instead')
            table = xr.DataArray(np.full((24, 5), 1),
                                 dims=['hour', 'kind'],
                                 coords={'hour': np.arange(0, 24, 1),
                                         'kind': kind}
                                 )

        ds[name] = xr.DataArray(np.full((24, len(ds.lat), len(ds.lon)), 0.),
                                dims=['hour', 'lat', 'lon']).rename(name)

        # multiply data by hourly factor and sum to total
        for k in kind:
            ds[name] += (ds[varname.split('_')[-2]+'_'+k] * table.sel(kind=k)).squeeze('time')

        # drop 'kind' variables
        ds = ds.drop_vars([key for key in list(ds.keys()) if 'E_' not in key])
        ds = ds.drop('kind')

        # add longitude and latitude variables
        lon2d, lat2d = np.meshgrid(ds.lon, ds.lat)

        ds['longitude'] = xr.DataArray(lon2d,
                                       coords=[ds.lat, ds.lon],
                                       dims=['y', 'x'])
        ds['latitude'] = xr.DataArray(lat2d,
                                      coords=[ds.lat, ds.lon],
                                      dims=['y', 'x'])

        # assign units
        if name == 'E_PM25':
            ds[name].attrs['units'] = 'ug/m3 m/s'
        elif name in ['E_NO', 'E_SO2']:
            ds[name].attrs['units'] = 'mol km-2 hr-1'

        return ds

    def perdelta(self, start, end, delta):
        '''Generate the 24-h datetime list'''
        curr = start
        while curr <= end:
            yield curr
            curr += delta

    def resample_WRF(self, st, et, delta):
        '''Create Times variable and resample emission species DataArray.'''
        # generate date every hour
        datetime_list = list(self.perdelta(st, et, timedelta(hours=1)))
        t_format = '%Y-%m-%d_%H:%M:%S'

        # convert datetime to date string
        Times = []
        for timstep in datetime_list:
            times_str = strftime(t_format, timstep.timetuple())
            Times.append(times_str)

        # the method of creating "Times" with unlimited dimension
        # ref: htttps://github.com/pydata/xarray/issues/3407
        Times = xr.DataArray(np.array(Times,
                                      dtype=np.dtype(('S', 19))
                                      ),
                             dims=['Time'])

        self.chemi = xr.Dataset({'Times': Times})

        # resample
        orig_def = SwathDefinition(lons=self.emi['longitude'],
                                   lats=self.emi['latitude'])
        for vname in self.emi.data_vars:
            if 'E_' in vname:
                logging.info(f'Resample {vname} ...')
                resampled_list = []
                for t in range(self.emi[vname].shape[0]):
                    # different resample methods
                    # see: http://earthpy.org/interpolation_between_grids_with_pyresample.html
                    if resample_method == 'nearest':
                        resampled_list.append(resample_nearest(
                                              orig_def,
                                              self.emi[vname][t, :, :].values,
                                              self.area_def,
                                              radius_of_influence=100000,
                                              fill_value=0.)
                                              )
                    elif resample_method == 'idw':
                        resampled_list.append(resample_custom(
                                              orig_def,
                                              self.emi[vname][t, :, :].values,
                                              self.area_def,
                                              radius_of_influence=100000,
                                              neighbours=10,
                                              weight_funcs=lambda r: 1/r**2,
                                              fill_value=0.)
                                              )
                    elif resample_method == 'bilinear':
                        resampled_list.append(resample_bilinear(
                                              self.emi[vname][t, :, :].values,
                                              orig_def,
                                              self.area_def,
                                              radius=100000,
                                              neighbours=10,
                                              nprocs=4,
                                              reduce_data=True,
                                              segments=None,
                                              fill_value=0.,
                                              epsilon=0)
                                              )
                # combine 2d array list to one 3d
                # ref: https://stackoverflow.com/questions/4341359/
                #       convert-a-list-of-2d-numpy-arrays-to-one-3d-numpy-array
                # we also need to flip the 3d array,
                #    because of the "strange" order of WRF.
                resampled_data = np.flip(np.rollaxis(np.dstack(resampled_list), -1), 1)[:, np.newaxis, ...]

                # assign to self.chemi with dims
                self.chemi[vname] = xr.DataArray(resampled_data,
                                                 dims=['Time',
                                                       'emissions_zdim',
                                                       'south_north',
                                                       'west_east'
                                                       ]
                                                 )

                # add attrs needed by WRF-Chem
                v_attrs = {'FieldType': 104,
                           'MemoryOrder': 'XYZ',
                           'description': vname,
                           'stagger': '',
                           'coordinates': 'XLONG XLAT',
                           'units': self.emi[vname].attrs['units']
                           }

                self.chemi[vname] = self.chemi[vname].assign_attrs(v_attrs)

                logging.debug(' '*8 +
                              ' min: ' + str(self.chemi[vname].min().values) +
                              ' max: ' + str(self.chemi[vname].max().values) +
                              ' mean ' + str(self.chemi[vname].mean().values)
                              )

    def replace_var(self, ):
        '''Replace variables in two wrfchemi* files: wrfchemi_00z_d<n> and wrfchemi_12z_d<n>'''
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        # set compression
        comp = dict(zlib=True, complevel=5)
        comp_t = dict(zlib=True, complevel=5, char_dim_name='DateStrLen')

        # two period
        tindex = [np.arange(12), np.arange(12, 24, 1)]

        # generate files
        for index, file in enumerate([output_dir+f'wrfchemi_00z_{domain}', output_dir+f'wrfchemi_12z_{domain}']):
            if os.path.isfile(file):
                ds = xr.open_dataset(file)
                ds['E_NO'] = self.chemi['E_NO'].isel(Time=tindex[index])
                # ds['E_PM_25'] = self.chemi['E_PM25'].isel(Time=tindex[index])
                ds['E_SO2'] = self.chemi['E_SO2'].isel(Time=tindex[index])

                encoding = {var: comp_t if var == 'Times' else comp
                            for var in ds.data_vars}

                logging.info(f'Saving to {file}')
                ds.to_netcdf(f'{file}',
                             format='NETCDF4',
                             encoding=encoding,
                             unlimited_dims={'Time': True}
                             )
            else:
                print('!!! Please run mozcart.py first !!!')

        logging.info('----- Successfully -----')


if __name__ == '__main__':
    st = datetime(yyyy, mm, dd, minhour)
    et = datetime(yyyy, mm, dd, maxhour)
    vito(st, et, delta)
