################################################################################
# Module: utils_geo.py
# Description: Geospatial utility functions.
# License: MIT, see full license in LICENSE.txt
# Web: https://github.com/gboeing/osmnx
################################################################################

import math
import networkx as nx
import numpy as np
import pandas as pd
import time
from collections import OrderedDict
from shapely.geometry import LineString
from shapely.geometry import MultiLineString
from shapely.geometry import MultiPoint
from shapely.geometry import MultiPolygon
from shapely.geometry import Point
from shapely.geometry import Polygon

from .downloader import nominatim_request
from .utils import log
from .utils_graph import graph_to_gdfs

# scipy and sklearn are optional dependencies for faster nearest node search
try:
    from scipy.spatial import cKDTree
except ImportError as e:
    cKDTree = None
try:
    from sklearn.neighbors import BallTree
except ImportError as e:
    BallTree = None





def geocode(query):
    """
    Geocode a query string to (lat, lon) with the Nominatim geocoder.

    Parameters
    ----------
    query : string
        the query string to geocode

    Returns
    -------
    point : tuple
        the (lat, lon) coordinates returned by the geocoder
    """

    # define the parameters
    params = OrderedDict()
    params['format'] = 'json'
    params['limit'] = 1
    params['dedupe'] = 0  # prevent OSM from deduping results so we get precisely 'limit' # of results
    params['q'] = query
    response_json = nominatim_request(params=params, timeout=30)

    # if results were returned, parse lat and long out of the result
    if len(response_json) > 0 and 'lat' in response_json[0] and 'lon' in response_json[0]:
        lat = float(response_json[0]['lat'])
        lon = float(response_json[0]['lon'])
        point = (lat, lon)
        log('Geocoded "{}" to {}'.format(query, point))
        return point
    else:
        raise Exception('Nominatim geocoder returned no results for query "{}"'.format(query))



def great_circle_vec(lat1, lng1, lat2, lng2, earth_radius=6371009):
    """
    Vectorized function to calculate the great-circle distance between two
    points or between vectors of points, using haversine.

    Parameters
    ----------
    lat1 : float or array of float
    lng1 : float or array of float
    lat2 : float or array of float
    lng2 : float or array of float
    earth_radius : numeric
        radius of earth in units in which distance will be returned (default is
        meters)

    Returns
    -------
    distance : float or vector of floats
        distance or vector of distances from (lat1, lng1) to (lat2, lng2) in
        units of earth_radius
    """

    phi1 = np.deg2rad(lat1)
    phi2 = np.deg2rad(lat2)
    d_phi = phi2 - phi1

    theta1 = np.deg2rad(lng1)
    theta2 = np.deg2rad(lng2)
    d_theta = theta2 - theta1

    h = np.sin(d_phi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(d_theta / 2) ** 2
    h = np.minimum(1.0, h)  # protect against floating point errors

    arc = 2 * np.arcsin(np.sqrt(h))

    # return distance in units of earth_radius
    distance = arc * earth_radius
    return distance


def euclidean_dist_vec(y1, x1, y2, x2):
    """
    Vectorized function to calculate the euclidean distance between two points
    or between vectors of points.

    Parameters
    ----------
    y1 : float or array of float
    x1 : float or array of float
    y2 : float or array of float
    x2 : float or array of float

    Returns
    -------
    distance : float or array of float
        distance or vector of distances from (x1, y1) to (x2, y2) in graph units
    """

    # euclid's formula
    distance = ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5
    return distance



def get_nearest_node(G, point, method='haversine', return_dist=False):
    """
    Return the graph node nearest to some specified (lat, lng) or (y, x) point,
    and optionally the distance between the node and the point. This function
    can use either a haversine or euclidean distance calculator.

    Parameters
    ----------
    G : networkx multidigraph
    point : tuple
        The (lat, lng) or (y, x) point for which we will find the nearest node
        in the graph
    method : str {'haversine', 'euclidean'}
        Which method to use for calculating distances to find nearest node.
        If 'haversine', graph nodes' coordinates must be in units of decimal
        degrees. If 'euclidean', graph nodes' coordinates must be projected.
    return_dist : bool
        Optionally also return the distance (in meters if haversine, or graph
        node coordinate units if euclidean) between the point and the nearest
        node.

    Returns
    -------
    int or tuple of (int, float)
        Nearest node ID or optionally a tuple of (node ID, dist), where dist is
        the distance (in meters if haversine, or graph node coordinate units
        if euclidean) between the point and nearest node
    """
    start_time = time.time()

    if not G or (G.number_of_nodes() == 0):
        raise ValueError('G argument must be not be empty or should contain at least one node')

    # dump graph node coordinates into a pandas dataframe indexed by node id
    # with x and y columns
    coords = [[node, data['x'], data['y']] for node, data in G.nodes(data=True)]
    df = pd.DataFrame(coords, columns=['node', 'x', 'y']).set_index('node')

    # add columns to the dataframe representing the (constant) coordinates of
    # the reference point
    df['reference_y'] = point[0]
    df['reference_x'] = point[1]

    # calculate the distance between each node and the reference point
    if method == 'haversine':
        # calculate distance vector using haversine (ie, for
        # spherical lat-long geometries)
        distances = great_circle_vec(lat1=df['reference_y'],
                                     lng1=df['reference_x'],
                                     lat2=df['y'],
                                     lng2=df['x'])

    elif method == 'euclidean':
        # calculate distance vector using euclidean distances (ie, for projected
        # planar geometries)
        distances = euclidean_dist_vec(y1=df['reference_y'],
                                       x1=df['reference_x'],
                                       y2=df['y'],
                                       x2=df['x'])

    else:
        raise ValueError('method argument must be either "haversine" or "euclidean"')

    # nearest node's ID is the index label of the minimum distance
    nearest_node = distances.idxmin()
    log('Found nearest node ({}) to point {} in {:,.2f} seconds'.format(nearest_node, point, time.time()-start_time))

    # if caller requested return_dist, return distance between the point and the
    # nearest node as well
    if return_dist:
        return nearest_node, distances.loc[nearest_node]
    else:
        return nearest_node



def get_nearest_edge(G, point, return_geom=False, return_dist=False):
    """
    Return the nearest edge to a point, by minimum euclidean distance.

    Parameters
    ----------
    G : networkx multidigraph
    point : tuple
        The (lat, lng) or (y, x) point for which we will find the nearest edge
        in the graph
    return_geom : bool
        Optionally return the geometry of the nearest edge
    return_dist : bool
        Optionally return the distance in graph's coordinates' units between the 
        point and the nearest node

    Returns
    -------
    tuple
        Graph edge unique identifier as a tuple of (u, v, key).
        Or a tuple of (u, v, key, geom) if return_geom is True.
        Or a tuple of (u, v, key, dist) if return_dist is True.
        Or a tuple of (u, v, key, geom, dist) if return_geom and return_dist are True.
    """
    start_time = time.time()

    # get u, v, key, geom from all the graph edges
    gdf_edges = graph_to_gdfs(G, nodes=False, fill_edge_geometry=True)
    edges = gdf_edges[['u', 'v', 'key', 'geometry']].values

    # convert lat-lng point to x-y for shapely distance operation
    xy_point = Point(reversed(point))

    # calculate euclidean distance from each edge's geometry to this point
    edge_distances = [(edge, xy_point.distance(edge[3])) for edge in edges]

    # the nearest edge minimizes the distance to the point
    (u, v, key, geom), dist = min(edge_distances, key=lambda x: x[1])
    log('Found nearest edge ({}) to point {} in {:,.2f} seconds'.format((u, v, key), point, time.time() - start_time))

    # return results requested by caller
    if return_dist and return_geom:
        return u, v, key, geom, dist
    elif return_dist:
        return u, v, key, dist
    elif return_geom:
        return u, v, key, geom
    else:
        return u, v, key



def get_nearest_nodes(G, X, Y, method=None):
    """
    Return the graph nodes nearest to a list of points. Pass in points
    as separate vectors of X and Y coordinates. The 'kdtree' method
    is by far the fastest with large data sets, but only finds approximate
    nearest nodes if working in unprojected coordinates like lat-lng (it
    precisely finds the nearest node if working in projected coordinates).
    The 'balltree' method is second fastest with large data sets, but it
    is precise if working in unprojected coordinates like lat-lng.

    Parameters
    ----------
    G : networkx multidigraph
    X : list-like
        The vector of longitudes or x's for which we will find the nearest
        node in the graph
    Y : list-like
        The vector of latitudes or y's for which we will find the nearest
        node in the graph
    method : str {None, 'kdtree', 'balltree'}
        Which method to use for finding nearest node to each point.
        If None, we manually find each node one at a time using
        osmnx.utils.get_nearest_node and haversine. If 'kdtree' we use
        scipy.spatial.cKDTree for very fast euclidean search. If
        'balltree', we use sklearn.neighbors.BallTree for fast
        haversine search.

    Returns
    -------
    nn : array
        list of nearest node IDs
    """

    start_time = time.time()

    if method is None:

        # calculate nearest node one at a time for each point
        nn = [get_nearest_node(G, (y, x), method='haversine') for x, y in zip(X, Y)]

    elif method == 'kdtree':

        # check if we were able to import scipy.spatial.cKDTree successfully
        if not cKDTree:
            raise ImportError('The scipy package must be installed to use this optional feature.')

        # build a k-d tree for euclidean nearest node search
        nodes = pd.DataFrame({'x':nx.get_node_attributes(G, 'x'),
                              'y':nx.get_node_attributes(G, 'y')})
        tree = cKDTree(data=nodes[['x', 'y']], compact_nodes=True, balanced_tree=True)

        # query the tree for nearest node to each point
        points = np.array([X, Y]).T
        dist, idx = tree.query(points, k=1)
        nn = nodes.iloc[idx].index

    elif method == 'balltree':

        # check if we were able to import sklearn.neighbors.BallTree successfully
        if not BallTree:
            raise ImportError('The scikit-learn package must be installed to use this optional feature.')

        # haversine requires data in form of [lat, lng] and inputs/outputs in units of radians
        nodes = pd.DataFrame({'x':nx.get_node_attributes(G, 'x'),
                              'y':nx.get_node_attributes(G, 'y')})
        nodes_rad = np.deg2rad(nodes[['y', 'x']].astype(np.float))
        points = np.array([Y.astype(np.float), X.astype(np.float)]).T
        points_rad = np.deg2rad(points)

        # build a ball tree for haversine nearest node search
        tree = BallTree(nodes_rad, metric='haversine')

        # query the tree for nearest node to each point
        idx = tree.query(points_rad, k=1, return_distance=False)
        nn = nodes.iloc[idx[:,0]].index

    else:
        raise ValueError('You must pass a valid method name, or None.')

    log('Found nearest nodes to {:,} points in {:,.2f} seconds'.format(len(X), time.time()-start_time))

    return np.array(nn)


def get_nearest_edges(G, X, Y, method=None, dist=0.0001):
    """
    Return the graph edges nearest to a list of points. Pass in points
    as separate vectors of X and Y coordinates. The 'kdtree' method
    is by far the fastest with large data sets, but only finds approximate
    nearest edges if working in unprojected coordinates like lat-lng (it
    precisely finds the nearest edge if working in projected coordinates).
    The 'balltree' method is second fastest with large data sets, but it
    is precise if working in unprojected coordinates like lat-lng. As a
    rule of thumb, if you have a small graph just use method=None. If you 
    have a large graph with lat-lng coordinates, use method='balltree'.
    If you have a large graph with projected coordinates, use 
    method='kdtree'. Note that if you are working in units of lat-lng,
    the X vector corresponds to longitude and the Y vector corresponds
    to latitude. The method creates equally distanced points along the edges 
    of the network. Then, these points are used in a kdTree or BallTree search
    to identify which is nearest.Note that this method will not give the exact
    perpendicular point along the edge, but the smaller the *dist* parameter,
    the closer the solution will be.

    Parameters
    ----------
    G : networkx multidigraph
    X : list-like
        The vector of longitudes or x's for which we will find the nearest
        edge in the graph. For projected graphs, use the projected coordinates,
        usually in meters.
    Y : list-like
        The vector of latitudes or y's for which we will find the nearest
        edge in the graph. For projected graphs, use the projected coordinates,
        usually in meters.
    method : str {None, 'kdtree', 'balltree'}
        Which method to use for finding nearest edge to each point.
        If None, we manually find each edge one at a time using
        osmnx.utils.get_nearest_edge. If 'kdtree' we use
        scipy.spatial.cKDTree for very fast euclidean search. Recommended for
        projected graphs. If 'balltree', we use sklearn.neighbors.BallTree for
        fast haversine search. Recommended for unprojected graphs.

    dist : float
        spacing length along edges. Units are the same as the geom; Degrees for
        unprojected geometries and meters for projected geometries. The smaller
        the value, the more points are created.

    Returns
    -------
    ne : ndarray
        array of nearest edges represented by their startpoint and endpoint ids,
        u and v, the OSM ids of the nodes, and the edge key.
    """
    start_time = time.time()

    if method is None:
        # calculate nearest edge one at a time for each (y, x) point
        ne = [get_nearest_edge(G, (y, x)) for x, y in zip(X, Y)]

    elif method == 'kdtree':

        # check if we were able to import scipy.spatial.cKDTree successfully
        if not cKDTree:
            raise ImportError('The scipy package must be installed to use this optional feature.')

        # transform graph into DataFrame
        edges = graph_to_gdfs(G, nodes=False, fill_edge_geometry=True)

        # transform edges into evenly spaced points
        edges['points'] = edges.apply(lambda x: redistribute_vertices(x.geometry, dist), axis=1)

        # develop edges data for each created points
        extended = edges['points'].apply([pd.Series]).stack().reset_index(level=1, drop=True).join(edges).reset_index()

        # Prepare btree arrays
        nbdata = np.array(list(zip(extended['Series'].apply(lambda x: x.x),
                                   extended['Series'].apply(lambda x: x.y))))

        # build a k-d tree for euclidean nearest node search
        btree = cKDTree(data=nbdata, compact_nodes=True, balanced_tree=True)

        # query the tree for nearest node to each point
        points = np.array([X, Y]).T
        dist, idx = btree.query(points, k=1)  # Returns ids of closest point
        eidx = extended.loc[idx, 'index']
        ne = edges.loc[eidx, ['u', 'v','key']]

    elif method == 'balltree':

        # check if we were able to import sklearn.neighbors.BallTree successfully
        if not BallTree:
            raise ImportError('The scikit-learn package must be installed to use this optional feature.')

        # transform graph into DataFrame
        edges = graph_to_gdfs(G, nodes=False, fill_edge_geometry=True)

        # transform edges into evenly spaced points
        edges['points'] = edges.apply(lambda x: redistribute_vertices(x.geometry, dist), axis=1)

        # develop edges data for each created points
        extended = edges['points'].apply([pd.Series]).stack().reset_index(level=1, drop=True).join(edges).reset_index()

        # haversine requires data in form of [lat, lng] and inputs/outputs in units of radians
        nodes = pd.DataFrame({'x': extended['Series'].apply(lambda x: x.x),
                              'y': extended['Series'].apply(lambda x: x.y)})
        nodes_rad = np.deg2rad(nodes[['y', 'x']].values.astype(np.float))
        points = np.array([Y, X]).T
        points_rad = np.deg2rad(points)

        # build a ball tree for haversine nearest node search
        tree = BallTree(nodes_rad, metric='haversine')

        # query the tree for nearest node to each point
        idx = tree.query(points_rad, k=1, return_distance=False)
        eidx = extended.loc[idx[:, 0], 'index']
        ne = edges.loc[eidx, ['u', 'v','key']]

    else:
        raise ValueError('You must pass a valid method name, or None.')

    log('Found nearest edges to {:,} points in {:,.2f} seconds'.format(len(X), time.time() - start_time))

    return np.array(ne)


def redistribute_vertices(geom, dist):
    """
    Redistribute the vertices on a projected LineString or MultiLineString. The distance
    argument is only approximate since the total distance of the linestring may not be
    a multiple of the preferred distance. This function works on only [Multi]LineString
    geometry types.

    Parameters
    ----------
    geom : LineString or MultiLineString
        a Shapely geometry
    dist : float
        spacing length along edges. Units are the same as the geom; Degrees for unprojected geometries and meters
        for projected geometries. The smaller the value, the more points are created.

    Returns
    -------
        list of Point geometries : list
    """
    if geom.geom_type == 'LineString':
        num_vert = int(round(geom.length / dist))
        if num_vert == 0:
            num_vert = 1
        return [geom.interpolate(float(n) / num_vert, normalized=True)
                for n in range(num_vert + 1)]
    elif geom.geom_type == 'MultiLineString':
        parts = [redistribute_vertices(part, dist)
                 for part in geom]
        return type(geom)([p for p in parts if not p])
    else:
        raise ValueError('unhandled geometry {}'.format(geom.geom_type))


def get_bearing(origin_point, destination_point):
    """
    Calculate the bearing between two lat-long points. Each tuple should
    represent (lat, lng) as decimal degrees.

    Parameters
    ----------
    origin_point : tuple
    destination_point : tuple

    Returns
    -------
    bearing : float
        the compass bearing in decimal degrees from the origin point
        to the destination point
    """

    if not (isinstance(origin_point, tuple) and isinstance(destination_point, tuple)):
        raise TypeError('origin_point and destination_point must be (lat, lng) tuples')

    # get latitudes and the difference in longitude, as radians
    lat1 = math.radians(origin_point[0])
    lat2 = math.radians(destination_point[0])
    diff_lng = math.radians(destination_point[1] - origin_point[1])

    # calculate initial bearing from -180 degrees to +180 degrees
    x = math.sin(diff_lng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - (math.sin(lat1) * math.cos(lat2) * math.cos(diff_lng))
    initial_bearing = math.atan2(x, y)

    # normalize initial bearing to 0 degrees to 360 degrees to get compass bearing
    initial_bearing = math.degrees(initial_bearing)
    bearing = (initial_bearing + 360) % 360

    return bearing



def add_edge_bearings(G):
    """
    Calculate the compass bearing from origin node to destination node for each
    edge in the directed graph then add each bearing as a new edge attribute.

    Parameters
    ----------
    G : networkx multidigraph

    Returns
    -------
    G : networkx multidigraph
    """

    for u, v, data in G.edges(keys=False, data=True):

        if u == v:
            # a self-loop has an undefined compass bearing
            data['bearing'] = np.nan

        else:
            # calculate bearing from edge's origin to its destination
            origin_point = (G.nodes[u]['y'], G.nodes[u]['x'])
            destination_point = (G.nodes[v]['y'], G.nodes[v]['x'])
            bearing = get_bearing(origin_point, destination_point)

            # round to thousandth of a degree
            data['bearing'] = round(bearing, 3)

    return G





def round_polygon_coords(p, precision):
    """
    Round the coordinates of a shapely Polygon to some decimal precision.

    Parameters
    ----------
    p : shapely Polygon
        the polygon to round the coordinates of
    precision : int
        decimal precision to round coordinates to

    Returns
    -------
    new_poly : shapely Polygon
        the polygon with rounded coordinates
    """

    # round the coordinates of the Polygon exterior
    new_exterior = [[round(x, precision) for x in c] for c in p.exterior.coords]

    # round the coordinates of the (possibly multiple, possibly none) Polygon interior(s)
    new_interiors = []
    for interior in p.interiors:
        new_interiors.append([[round(x, precision) for x in c] for c in interior.coords])

    # construct a new Polygon with the rounded coordinates
    # buffer by zero to clean self-touching or self-crossing polygons
    new_poly = Polygon(shell=new_exterior, holes=new_interiors).buffer(0)
    return new_poly



def round_multipolygon_coords(mp, precision):
    """
    Round the coordinates of a shapely MultiPolygon to some decimal precision.

    Parameters
    ----------
    mp : shapely MultiPolygon
        the MultiPolygon to round the coordinates of
    precision : int
        decimal precision to round coordinates to

    Returns
    -------
    MultiPolygon
    """

    return MultiPolygon([round_polygon_coords(p, precision) for p in mp])



def round_point_coords(pt, precision):
    """
    Round the coordinates of a shapely Point to some decimal precision.

    Parameters
    ----------
    pt : shapely Point
        the Point to round the coordinates of
    precision : int
        decimal precision to round coordinates to

    Returns
    -------
    Point
    """

    return Point([round(x, precision) for x in pt.coords[0]])



def round_multipoint_coords(mpt, precision):
    """
    Round the coordinates of a shapely MultiPoint to some decimal precision.

    Parameters
    ----------
    mpt : shapely MultiPoint
        the MultiPoint to round the coordinates of
    precision : int
        decimal precision to round coordinates to

    Returns
    -------
    MultiPoint
    """

    return MultiPoint([round_point_coords(pt, precision) for pt in mpt])



def round_linestring_coords(ls, precision):
    """
    Round the coordinates of a shapely LineString to some decimal precision.

    Parameters
    ----------
    ls : shapely LineString
        the LineString to round the coordinates of
    precision : int
        decimal precision to round coordinates to

    Returns
    -------
    LineString
    """

    return LineString([[round(x, precision) for x in c] for c in ls.coords])



def round_multilinestring_coords(mls, precision):
    """
    Round the coordinates of a shapely MultiLineString to some decimal precision.

    Parameters
    ----------
    mls : shapely MultiLineString
        the MultiLineString to round the coordinates of
    precision : int
        decimal precision to round coordinates to

    Returns
    -------
    MultiLineString
    """

    return MultiLineString([round_linestring_coords(ls, precision) for ls in mls])



def round_shape_coords(shape, precision):
    """
    Round the coordinates of a shapely geometry to some decimal precision.

    Parameters
    ----------
    shape : shapely geometry, one of Point, MultiPoint, LineString,
            MultiLineString, Polygon, or MultiPolygon
        the geometry to round the coordinates of
    precision : int
        decimal precision to round coordinates to

    Returns
    -------
    shapely geometry
    """

    if isinstance(shape, Point):
        return round_point_coords(shape, precision)

    elif isinstance(shape, MultiPoint):
        return round_multipoint_coords(shape, precision)

    elif isinstance(shape, LineString):
        return round_linestring_coords(shape, precision)

    elif isinstance(shape, MultiLineString):
        return round_multilinestring_coords(shape, precision)

    elif isinstance(shape, Polygon):
        return round_polygon_coords(shape, precision)

    elif isinstance(shape, MultiPolygon):
        return round_multipolygon_coords(shape, precision)

    else:
        raise TypeError('cannot round coordinates of unhandled geometry type: {}'.format(type(shape)))



def bbox_to_poly(north, south, east, west):
    """
    Convenience function to parse bbox -> poly
    """

    return Polygon([(west, south), (east, south), (east, north), (west, north)])



