import argparse
from itertools import combinations
import json
import math
import warnings
import os
import sys

import sqlalchemy

from geoalchemy2.shape import to_shape
import numpy as np
import pandas as pd
import shapely
from redis import StrictRedis
import yaml

#Load the config file
with open(os.environ['autocnet_config'], 'r') as f:
    config = yaml.load(f)

# Patch in dev. versions if requested.
acp = config.get('developer', {}).get('autocnet_path', None)
if acp:
    sys.path.insert(0, acp)

asp = config.get('developer', {}).get('autocnet_server_path', None)
if asp:
    sys.path.insert(0, acp)

from autocnet.io.keypoints import from_hdf
from autocnet.transformation.fundamental_matrix import compute_fundamental_matrix, compute_reprojection_error
from autocnet.utils.utils import make_homogeneous

from autocnet_server.db.model import Images, Keypoints, Matches, Cameras, Network, Base, Overlay
from autocnet_server.db.connection import new_connection


def spatial_suppression(df, bounds, xkey='lon', ykey='lat', k=60, error_k=0.05, nsteps=250):
    #TODO: Push this more generalized algorithm back into AutoCNet

    # Compute the bounding area inside of which the suppression will be applied
    minx = min(bounds[0], bounds[2])
    maxx = max(bounds[0], bounds[2])
    miny = min(bounds[1], bounds[3])
    maxy = max(bounds[1], bounds[3])
    domain = (maxx-minx),(maxy-miny)
    min_radius = min(domain) / 20
    max_radius = max(domain)
    search_space = np.linspace(min_radius, max_radius, nsteps)
    cell_sizes = search_space / math.sqrt(2)
    min_idx = 0
    max_idx = len(search_space) - 1

    # Setup flags to watch for looping
    prev_min = None
    prev_max = None

    # Sort the dataframe (hard coded to ascending as lower reproj error is better)
    df = df.sort_values(by=['strength'], ascending=True).copy()
    df = df.reset_index(drop=True)
    mask = pd.Series(False, index=df.index)

    process = True
    result = []
    while process:
        # Binary search
        mid_idx = int((min_idx + max_idx) / 2)
        if min_idx == mid_idx or mid_idx == max_idx:
            warnings.warn('Unable to optimally solve.')
            process = False
        else:
            # Setup to store results
            result = []

        # Get the current cell size and grid the domain
        cell_size = cell_sizes[mid_idx]
        n_x_cells = int(round(domain[0] / cell_size, 0)) - 1
        n_y_cells = int(round(domain[1] / cell_size, 0)) - 1

        if n_x_cells <= 0:
            n_x_cells = 1
        if n_y_cells <= 0:
            n_y_cells = 1

        grid = np.zeros((n_y_cells, n_x_cells), dtype=np.bool)
        # Assign all points to bins
        x_edges = np.linspace(minx, maxx, n_x_cells)
        y_edges = np.linspace(miny, maxy, n_y_cells)
        xbins = np.digitize(df['lon'], bins=x_edges)
        ybins = np.digitize(df['lat'], bins=y_edges)

        # Starting with the best point, start assigning points to grid cells
        for i, (idx, p) in enumerate(df.iterrows()):
            x_center = xbins[i] - 1
            y_center = ybins[i] - 1
            cell = grid[y_center, x_center]

            if cell == False:
                result.append(idx)
                # Set the cell to True
                grid[y_center, x_center] = True

            # If everything is already 'covered' break from the list
            if grid.all() == False:
                continue

        # Check to see if the algorithm is completed, or if the grid size needs to be larger or smaller
        if k - k * error_k <= len(result) <= k + k * error_k:
            # Success, in bounds
            process = False
        elif len(result) < k - k * error_k:
            # The radius is too large
            max_idx = mid_idx
            if max_idx == 0:
                warnings.warn('Unable to retrieve {} points. Consider reducing the amount of points you request(k)'
                            .format(k))
                process = False
            if min_idx == max_idx:
                process = False
        elif len(result) > k + k * error_k:
            # Too many points, break
            min_idx = mid_idx

    mask.loc[list(result)] = True
    tp = df[mask]
    return tp

def deepen(matches, fundamentals, overlaps, oid):

    points = []
    for g, subm in matches.groupby(['source', 'destination']):
        w = int(g[0])
        v = int(g[1])

        push_into = [i for i in overlaps if i not in g]

        x1 = make_homogeneous(subm[['source_x', 'source_y']].values)
        x2 = make_homogeneous(subm[['destination_x', 'destination_y']].values)

        pid = 0
        for i in range(x1.shape[0]):
            row = subm.iloc[i]
            geom  = 'SRID=949900;POINTZ({} {} {})'.format(row.lon, row.lat, 0)
            a = x1[i]
            b = x2[i]

            p1 = {'image_id':w,
                  'keypoint_id':int(row.source_idx),
                  'x':float(a[0]), 'y':float(a[1]),
                  'match_id':int(row.name),
                  'point_id':'{}_{}_{}'.format(oid, g, pid),
                  'geom':geom}
            p2 = {'image_id':v,
                  'keypoint_id':int(row.destination_idx),
                  'x':float(b[0]), 'y':float(b[1]),
                  'match_id':int(row.name),
                  'point_id':'{}_{}_{}'.format(oid, g, pid),
                  'geom':geom}

            points.append(p1)
            points.append(p2)

            for e in push_into:
                try:
                    if w > e:
                        f31 = [e,w]
                        f31 = np.asarray(fundamentals[tuple(f31)]).T
                    else:
                        f31 = [w,e]
                        f31 = np.asarray(fundamentals[tuple(f31)])

                    if v > e:
                        f32 = [e,v]
                        f32 = np.asarray(fundamentals[tuple(f32)]).T
                    else:
                        f32 = [v,e]
                        f32 = np.asarray(fundamentals[tuple(f32)])

                    x3 = np.cross(f31.dot(a), f32.dot(b))
                    x3[0] /= x3[2]
                    x3[1] /= x3[2]

                    # This needs to aggregate all of the
                    n = {'image_id':e,
                        'keypoint_id':None,
                        'x':float(x3[0]), 'y':float(x3[1]),
                        'point_id':'{}_{}_{}'.format(oid, g, pid),
                        'geom':geom, 'match_id':None}

                    points.append(n)
                except:
                    pass
            pid += 1
    return points

def main(msg):
    oid = msg['oid']
    session = create_session()
    overlay_row = session.query(Overlay).get(oid)
    overlaps = sorted(overlay_row.overlaps)

    footprint = to_shape(overlay_row.geom)
    files = session.query(Keypoints).filter(Keypoints.image_id.in_(overlaps)).all()
    files = {i.image_id:i.path for i in files}

    # Compute the fundamental matrices
    fundamentals = {}
    matches = []

    mquery = session.query(Matches)
    for e in combinations(overlaps, 2):
        qf = mquery.filter(Matches.source == e[0],
                      Matches.destination == e[1])
        match = pd.read_sql(qf.statement, mquery.session.bind)
        if len(match) < 7:
            continue
        s = match.iloc[0].source
        d = match.iloc[0].destination
        source_path = files[e[0]]
        destination_path = files[e[1]]

        x1 = from_hdf(source_path, index=match.source_idx.values, descriptors=False)
        x2 = from_hdf(destination_path, index=match.destination_idx.values, descriptors=False)

        x1 = make_homogeneous(x1[['x', 'y']].values)
        x2 = make_homogeneous(x2[['x', 'y']].values)
        f, fmask = compute_fundamental_matrix(x1, x2, method='ransac', reproj_threshold=20)
        fundamentals[e] = f
        match['strength'] = compute_reprojection_error(f, x1, x2)
        matches.append(match)
    session.close()
    matches = pd.concat(matches)

    # Of the concatenated matches only a subset intersect the geometry for this overlap, pull these
    def check_in(r, poly):
        p = shapely.geometry.Point(r.lon, r.lat)
        return p.within(poly)

    intersects = matches.apply(check_in, args=(footprint,), axis=1)
    matches = matches[intersects]
    matches = matches.reset_index(drop=True)

    # Apply the spatial suppression
    bounds = footprint.bounds
    k = footprint.area / 0.005
    if k < 3:
        k = 3
    if k > 25:
        k = 25
    subset = spatial_suppression(matches, bounds, k=k)

    # Push the points through
    pts = deepen(subset, fundamentals, overlaps, oid)

    return pts

def finalize(data, queue):
    for k, v in data.items():
        if isinstance(v, np.ndarray):
            data[k] = v.tolist()

    queue.put(data)

def write_to_db(pts):
    to_add = []
    for p in pts:
        n = Network(image_id=p['image_id'],
                    keypoint_id=p.get('keypoint_id', None),
                    x = p['x'], y = p['y'],
                    match_id = p.get('match_id', None),
                    point_id = p.get('point_id', None),
                    geom = p['geom'])
        to_add.append(n)
    session = new_connection()
    session.begin()
    session.bulk_save_objects(to_add)
    session.commit()
    session.close()
    
if __name__ == '__main__':
    queue = StrictRedis(host="smalls", port=8000, db=0)
    msg = json.loads(queue.rpoplpush(config['redis']['processing_queue'],
                                     config['redis']['working_queue']))

    data = {}
    pts = main(msg)
    write_to_db(pts)
    #data['points'] = pts
    #data['success'] = True
    #data['callback'] = 'create_network_callback'

    #write_to_database(pts)

    #cafinalize(data, fqueue)
