import numpy as np
import matplotlib.pylab as plt
import healpy as hp
from lsst.sims.featureScheduler.modelObservatory import Model_observatory
from lsst.sims.featureScheduler.schedulers import Core_scheduler
from lsst.sims.featureScheduler.utils import standard_goals, create_season_offset, ra_dec_hp_map
import lsst.sims.featureScheduler.basis_functions as bf
from lsst.sims.featureScheduler.surveys import (generate_dd_surveys, Greedy_survey,
                                                Blob_survey)
from lsst.sims.featureScheduler import sim_runner
import lsst.sims.featureScheduler.detailers as detailers
import sys
import subprocess
import os
import argparse

from lsst.utils import getPackageDir


def big_sky_dust(nside=32, weights={'u': [0.31, 0.15, False], 'g': [0.44, 0.15],
                 'r': [1., 0.3], 'i': [1., 0.3], 'z': [0.9, 0.3],
                 'y': [0.9, 0.3, False]}, dust_limit=0.19):
    """
    Based on the Olsen et al Cadence White Paper
    """

    ebvDataDir = getPackageDir('sims_maps')
    filename = 'DustMaps/dust_nside_%i.npz' % nside
    dustmap = np.load(os.path.join(ebvDataDir, filename))['ebvMap']

    # wfd covers -72.25 < dec < 12.4. Avoid galactic plane |b| > 15 deg
    wfd_north = np.radians(12.4)
    wfd_south = np.radians(-72.25)
    full_north = np.radians(30.)

    ra, dec = ra_dec_hp_map(nside=nside)
    total_map = np.zeros(ra.size)

    # let's make a first pass here

    total_map[np.where(dec < full_north)] = 1e-6
    total_map[np.where((dec > wfd_south) &
                       (dec < wfd_north) &
                       (dustmap < dust_limit))] = 1.

    # Now let's break it down by filter
    result = {}

    for key in weights:
        result[key] = total_map + 0.
        result[key][np.where(result[key] == 1)] = weights[key][0]
        result[key][np.where(result[key] == 1e-6)] = weights[key][1]
        if len(weights[key]) == 3:
            result[key][np.where(dec > wfd_north)] = 0.

    return result


def wfd_half(target_map=None):
    """return Two maps that split the WFD in two dec bands
    """
    if target_map is None:
        sg = big_sky_dust()
        target_map = sg['r'] + 0
    wfd_pix = np.where(target_map == 1)[0]
    wfd_map = target_map*0
    wfd_map[wfd_pix] = 1
    wfd_halves = slice_wfd_area(2, {'r': wfd_map}, scale_down_factor=0)
    result = [-wfd_halves[0]['r'], -wfd_halves[1]['r']]
    return result


def slice_wfd_area(nslice, target_map, scale_down_factor=0.2):
    """
    Slice the WFD area into even dec bands
    """
    # Make it so things still sum to one.
    scale_up_factor = nslice - scale_down_factor*(nslice-1)

    wfd = target_map['r'] * 0
    wfd_indices = np.where(target_map['r'] == 1)[0]
    wfd[wfd_indices] = 1
    wfd_accum = np.cumsum(wfd)
    split_wfd_indices = np.floor(np.max(wfd_accum)/nslice*(np.arange(nslice)+1)).astype(int)
    split_wfd_indices = split_wfd_indices.tolist()
    split_wfd_indices = [0] + split_wfd_indices

    all_scaled_down = {}
    for filtername in target_map:
        all_scaled_down[filtername] = target_map[filtername]+0
        all_scaled_down[filtername][wfd_indices] *= scale_down_factor

    scaled_maps = []
    for i in range(len(split_wfd_indices)-1):
        new_map = {}
        indices = wfd_indices[split_wfd_indices[i]:split_wfd_indices[i+1]]
        for filtername in all_scaled_down:
            new_map[filtername] = all_scaled_down[filtername] + 0
            new_map[filtername][indices] = target_map[filtername][indices]*scale_up_factor
        scaled_maps.append(new_map)

    return scaled_maps


def slice_wfd_area_quad(target_map, scale_down_factor=0.2):
    """
    Make a fancy 4-stripe target map
    """
    # Make it so things still sum to one.
    nslice = 2
    scale_up_factor = nslice - scale_down_factor*(nslice-1)
    nslice2 = nslice * 2

    wfd = target_map['r'] * 0
    wfd_indices = np.where(target_map['r'] == 1)[0]
    wfd[wfd_indices] = 1
    wfd_accum = np.cumsum(wfd)
    split_wfd_indices = np.floor(np.max(wfd_accum)/nslice2*(np.arange(nslice2)+1)).astype(int)
    split_wfd_indices = split_wfd_indices.tolist()
    split_wfd_indices = [0] + split_wfd_indices

    all_scaled_down = {}
    for filtername in target_map:
        all_scaled_down[filtername] = target_map[filtername]+0
        all_scaled_down[filtername][wfd_indices] *= scale_down_factor

    scaled_maps = []
    for i in range(nslice):
        new_map = {}
        indices = wfd_indices[split_wfd_indices[i]:split_wfd_indices[i+1]]
        for filtername in all_scaled_down:
            new_map[filtername] = all_scaled_down[filtername] + 0
            new_map[filtername][indices] = target_map[filtername][indices]*scale_up_factor
        indices = wfd_indices[split_wfd_indices[i+2]:split_wfd_indices[i+2+1]]
        for filtername in all_scaled_down:
            new_map[filtername][indices] = target_map[filtername][indices]*scale_up_factor
        scaled_maps.append(new_map)

    return scaled_maps


def gen_greedy_surveys(nside=32, season_modulo=None, day_offset=None, max_season=10,
                       footprints=None, all_footprints_sum=None, all_rolling_sum=None,
                       nexp=1, exptime=30., filters=['r', 'i', 'z', 'y'],
                       camera_rot_limits=[-80., 80.],
                       shadow_minutes=60., max_alt=76., moon_distance=30., ignore_obs='DD',
                       m5_weight=3., footprint_weight=0.3, slewtime_weight=3.,
                       stayfilter_weight=3., roll_weight=3.):
    """
    Make a quick set of greedy surveys

    This is a convienence function to generate a list of survey objects that can be used with
    lsst.sims.featureScheduler.schedulers.Core_scheduler.
    To ensure we are robust against changes in the sims_featureScheduler codebase, all kwargs are
    explicitly set.

    Parameters
    ----------
    nside : int (32)
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filters : list of str (['r', 'i', 'z', 'y'])
        Which filters to generate surveys for.
    camera_rot_limits : list of float ([-87., 87.])
        The limits to impose when rotationally dithering the camera (degrees).
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    """
    # Define the extra parameters that are used in the greedy survey. I
    # think these are fairly set, so no need to promote to utility func kwargs
    greed_survey_params = {'block_size': 1, 'smoothing_kernel': None,
                           'seed': 42, 'camera': 'LSST', 'dither': True,
                           'survey_name': 'greedy'}

    surveys = []
    detailer = detailers.Camera_rot_detailer(min_rot=np.min(camera_rot_limits), max_rot=np.max(camera_rot_limits))
    wfd_halves = wfd_half()
    for filtername in filters:
        bfs = []
        bfs.append((bf.M5_diff_basis_function(filtername=filtername, nside=nside), m5_weight))
        fps = [footprint[filtername] for footprint in footprints]
        bfs.append((bf.Footprint_rolling_basis_function(filtername=filtername,
                                                        footprints=fps,
                                                        out_of_bounds_val=np.nan, nside=nside,
                                                        all_footprints_sum=all_footprints_sum, all_rolling_sum=all_rolling_sum,
                                                        day_offset=day_offset, season_modulo=season_modulo,
                                                        max_season=max_season), footprint_weight))
        bfs.append((bf.Slewtime_basis_function(filtername=filtername, nside=nside), slewtime_weight))
        bfs.append((bf.Strict_filter_basis_function(filtername=filtername), stayfilter_weight))
        bfs.append((bf.Map_modulo_basis_function(wfd_halves), roll_weight))
        # Masks, give these 0 weight
        bfs.append((bf.Zenith_shadow_mask_basis_function(nside=nside, shadow_minutes=shadow_minutes,
                                                         max_alt=max_alt), 0))
        bfs.append((bf.Moon_avoidance_basis_function(nside=nside, moon_distance=moon_distance), 0))

        bfs.append((bf.Filter_loaded_basis_function(filternames=filtername), 0))
        bfs.append((bf.Planet_mask_basis_function(nside=nside), 0))

        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        surveys.append(Greedy_survey(basis_functions, weights, exptime=exptime, filtername=filtername,
                                     nside=nside, ignore_obs=ignore_obs, nexp=nexp,
                                     detailers=[detailer], **greed_survey_params))

    return surveys


def generate_blobs(nside, nexp=1, season_modulo=None, day_offset=None, max_season=10,
                   footprints=None, all_footprints_sum=None, all_rolling_sum=None,
                   exptime=30., filter1s=['u', 'g', 'r', 'i', 'z', 'y'],
                   filter2s=[None, 'r', 'i', 'z', None, None], pair_time=22.,
                   camera_rot_limits=[-80., 80.], n_obs_template=3,
                   season=300., season_start_hour=-4., season_end_hour=2.,
                   shadow_minutes=60., max_alt=76., moon_distance=30., ignore_obs='DD',
                   m5_weight=6., footprint_weight=0.6, slewtime_weight=3.,
                   stayfilter_weight=3., template_weight=12., roll_weight=3.):
    """
    Generate surveys that take observations in blobs.

    Parameters
    ----------
    nside : int (32)
        The HEALpix nside to use
    nexp : int (1)
        The number of exposures to use in a visit.
    exptime : float (30.)
        The exposure time to use per visit (seconds)
    filter1s : list of str
        The filternames for the first set
    filter2s : list of str
        The filter names for the second in the pair (None if unpaired)
    pair_time : float (22)
        The ideal time between pairs (minutes)
    camera_rot_limits : list of float ([-80., 80.])
        The limits to impose when rotationally dithering the camera (degrees).
    n_obs_template : int (3)
        The number of observations to take every season in each filter
    season : float (300)
        The length of season (i.e., how long before templates expire) (days)
    season_start_hour : float (-4.)
        For weighting how strongly a template image needs to be observed (hours)
    sesason_end_hour : float (2.)
        For weighting how strongly a template image needs to be observed (hours)
    shadow_minutes : float (60.)
        Used to mask regions around zenith (minutes)
    max_alt : float (76.
        The maximium altitude to use when masking zenith (degrees)
    moon_distance : float (30.)
        The mask radius to apply around the moon (degrees)
    ignore_obs : str or list of str ('DD')
        Ignore observations by surveys that include the given substring(s).
    m5_weight : float (3.)
        The weight for the 5-sigma depth difference basis function
    footprint_weight : float (0.3)
        The weight on the survey footprint basis function.
    slewtime_weight : float (3.)
        The weight on the slewtime basis function
    stayfilter_weight : float (3.)
        The weight on basis function that tries to stay avoid filter changes.
    template_weight : float (12.)
        The weight to place on getting image templates every season
    """

    blob_survey_params = {'slew_approx': 7.5, 'filter_change_approx': 140.,
                          'read_approx': 2., 'min_pair_time': 15., 'search_radius': 30.,
                          'alt_max': 85., 'az_range': 90., 'flush_time': 30.,
                          'smoothing_kernel': None, 'nside': nside, 'seed': 42, 'dither': True,
                          'twilight_scale': True}

    surveys = []
    wfd_halves = wfd_half()
    times_needed = [pair_time, pair_time*2]
    for filtername, filtername2 in zip(filter1s, filter2s):
        detailer_list = []
        detailer_list.append(detailers.Camera_rot_detailer(min_rot=np.min(camera_rot_limits),
                                                           max_rot=np.max(camera_rot_limits)))
        detailer_list.append(detailers.Close_alt_detailer())
        # List to hold tuples of (basis_function_object, weight)
        bfs = []

        if filtername2 is not None:
            bfs.append((bf.M5_diff_basis_function(filtername=filtername, nside=nside), m5_weight/2.))
            bfs.append((bf.M5_diff_basis_function(filtername=filtername2, nside=nside), m5_weight/2.))

        else:
            bfs.append((bf.M5_diff_basis_function(filtername=filtername, nside=nside), m5_weight))

        if filtername2 is not None:
            fps = [footprint[filtername] for footprint in footprints]
            bfs.append((bf.Footprint_rolling_basis_function(filtername=filtername,
                                                            footprints=fps,
                                                            out_of_bounds_val=np.nan, nside=nside,
                                                            all_footprints_sum=all_footprints_sum,
                                                            all_rolling_sum=all_rolling_sum,
                                                            day_offset=day_offset, season_modulo=season_modulo,
                                                            max_season=max_season), footprint_weight/2.))
            fps = [footprint[filtername2] for footprint in footprints]
            bfs.append((bf.Footprint_rolling_basis_function(filtername=filtername2,
                                                            footprints=fps,
                                                            out_of_bounds_val=np.nan, nside=nside,
                                                            all_footprints_sum=all_footprints_sum,
                                                            all_rolling_sum=all_rolling_sum,
                                                            day_offset=day_offset, season_modulo=season_modulo,
                                                            max_season=max_season), footprint_weight/2.))
        else:
            fps = [footprint[filtername] for footprint in footprints]
            bfs.append((bf.Footprint_rolling_basis_function(filtername=filtername,
                                                            footprints=fps,
                                                            out_of_bounds_val=np.nan, nside=nside,
                                                            all_footprints_sum=all_footprints_sum,
                                                            all_rolling_sum=all_rolling_sum,
                                                            season_modulo=season_modulo,
                                                            day_offset=day_offset, max_season=max_season), footprint_weight))

        bfs.append((bf.Slewtime_basis_function(filtername=filtername, nside=nside), slewtime_weight))
        bfs.append((bf.Strict_filter_basis_function(filtername=filtername), stayfilter_weight))

        if filtername2 is not None:
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername, nside=nside,
                                                         footprint=footprints[-1][filtername],
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight/2.))
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername2, nside=nside,
                                                         footprint=footprints[-1][filtername2],
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight/2.))
        else:
            bfs.append((bf.N_obs_per_year_basis_function(filtername=filtername, nside=nside,
                                                         footprint=footprints[-1][filtername],
                                                         n_obs=n_obs_template, season=season,
                                                         season_start_hour=season_start_hour,
                                                         season_end_hour=season_end_hour), template_weight))
        bfs.append((bf.Map_modulo_basis_function(wfd_halves), roll_weight))
        # Masks, give these 0 weight
        bfs.append((bf.Zenith_shadow_mask_basis_function(nside=nside, shadow_minutes=shadow_minutes, max_alt=max_alt,
                                                         penalty=np.nan, site='LSST'), 0.))
        bfs.append((bf.Moon_avoidance_basis_function(nside=nside, moon_distance=moon_distance), 0.))
        filternames = [fn for fn in [filtername, filtername2] if fn is not None]
        bfs.append((bf.Filter_loaded_basis_function(filternames=filternames), 0))
        if filtername2 is None:
            time_needed = times_needed[0]
        else:
            time_needed = times_needed[1]
        bfs.append((bf.Time_to_twilight_basis_function(time_needed=time_needed), 0.))
        bfs.append((bf.Not_twilight_basis_function(), 0.))
        bfs.append((bf.Planet_mask_basis_function(nside=nside), 0.))

        # unpack the basis functions and weights
        weights = [val[1] for val in bfs]
        basis_functions = [val[0] for val in bfs]
        if filtername2 is None:
            survey_name = 'blob, %s' % filtername
        else:
            survey_name = 'blob, %s%s' % (filtername, filtername2)
        if filtername2 is not None:
            detailer_list.append(detailers.Take_as_pairs_detailer(filtername=filtername2))
        surveys.append(Blob_survey(basis_functions, weights, filtername1=filtername, filtername2=filtername2,
                                   exptime=exptime,
                                   ideal_pair_time=pair_time,
                                   survey_note=survey_name, ignore_obs=ignore_obs,
                                   nexp=nexp, detailers=detailer_list, **blob_survey_params))

    return surveys


def run_sched(surveys, survey_length=365.25, nside=32, fileroot='baseline_', verbose=False,
              extra_info=None):
    years = np.round(survey_length/365.25)
    scheduler = Core_scheduler(surveys, nside=nside)
    n_visit_limit = None
    observatory = Model_observatory(nside=nside)
    observatory, scheduler, observations = sim_runner(observatory, scheduler,
                                                      survey_length=survey_length,
                                                      filename=fileroot+'%iyrs.db' % years,
                                                      delete_past=True, n_visit_limit=n_visit_limit,
                                                      verbose=verbose, extra_info=extra_info)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", dest='verbose', action='store_true')
    parser.set_defaults(verbose=False)
    parser.add_argument("--survey_length", type=float, default=365.25*10)
    parser.add_argument("--outDir", type=str, default="")
    parser.add_argument("--maxDither", type=float, default=0.7, help="Dither size for DDFs (deg)")
    parser.add_argument("--splits", type=int, default=2)
    parser.add_argument("--scale_down_factor", type=float, default=0.2)

    args = parser.parse_args()
    survey_length = args.survey_length  # Days
    outDir = args.outDir
    verbose = args.verbose
    max_dither = args.maxDither
    mod_year = args.splits
    scale_down_factor = args.scale_down_factor

    nside = 32
    per_night = True  # Dither DDF per night
    nexp = 1  # All observations
    mixed_pairs = True  # For the blob scheduler
    camera_ddf_rot_limit = 80.

    extra_info = {}
    exec_command = ''
    for arg in sys.argv:
        exec_command += ' ' + arg
    extra_info['exec command'] = exec_command
    try:
        extra_info['git hash'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'])
    except subprocess.CalledProcessError:
        extra_info['git hash'] = 'Not in git repo'

    extra_info['file executed'] = os.path.realpath(__file__)

    fileroot = 'alt_roll_mod%i_dust_sdf_%.2f_' % (mod_year, scale_down_factor)
    file_end = 'v1.4_'

    # Mark position of the sun at the start of the survey. Usefull for rolling cadence.
    observatory = Model_observatory(nside=nside)
    conditions = observatory.return_conditions()
    sun_ra_0 = conditions.sunRA  # radians
    offset = create_season_offset(nside, sun_ra_0)
    max_season = 6

    # Set up the DDF surveys to dither
    dither_detailer = detailers.Dither_detailer(per_night=per_night, max_dither=max_dither)
    details = [detailers.Camera_rot_detailer(min_rot=-camera_ddf_rot_limit, max_rot=camera_ddf_rot_limit), dither_detailer]
    ddfs = generate_dd_surveys(nside=nside, nexp=nexp, detailers=details)

    # Set up rolling maps
    sg = big_sky_dust()
    roll_maps = slice_wfd_area_quad(sg, scale_down_factor=scale_down_factor)
    footprints = roll_maps + [sg]

    all_footprints_sum = 0
    all_rolling_sum = 0

    wfd_indx = np.where(sg['r'] == 1)
    for fp in sg:
        all_footprints_sum += np.sum(sg[fp])
        all_rolling_sum += np.sum(sg[fp][wfd_indx])

    greedy = gen_greedy_surveys(nside, nexp=nexp, season_modulo=mod_year, day_offset=offset, footprints=footprints,
                                max_season=max_season, all_footprints_sum=all_footprints_sum, all_rolling_sum=all_rolling_sum)
    blobs = generate_blobs(nside, nexp=nexp, footprints=footprints,
                           season_modulo=mod_year, max_season=max_season, day_offset=offset,
                           all_footprints_sum=all_footprints_sum, all_rolling_sum=all_rolling_sum,)
    surveys = [ddfs, blobs, greedy]
    run_sched(surveys, survey_length=survey_length, verbose=verbose,
              fileroot=os.path.join(outDir, fileroot+file_end), extra_info=extra_info,
              nside=nside)
