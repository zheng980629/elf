from copy import deepcopy
import numpy as np

from .import features as elf_feats
from .import learning as elf_learn
from .import multicut as elf_mc
from .import watershed as elf_ws

FEATURE_NAMES = {'raw-edge-features', 'raw-region-features', 'boundary-edge-features'}
DEFAULT_WS_KWARGS = {'threshold': 0.25, 'sigma_seeds': 2., 'sigma_weights': 2.,
                     'min_size': 100, 'alpha': 0.9, 'pixel_pitch': None,
                     'apply_nonmax_suppression': False}
DEFAULT_RF_KWARGS = {'ignore_label': None, 'n_estimators': 200, 'max_depth': 10}


# TODO:
# - implement simple multicut workflow
# - lmc workflows
# - multicut_workflow_from_config(config_file)
# - add callbacks to customize features, costs etc. to workflows for more customization


def _compute_watershed(boundaries, use_2dws, ws_kwargs, n_threads):
    if use_2dws:
        return elf_ws.stacked_watershed(boundaries, n_threads=n_threads, **ws_kwargs)[0]
    else:
        return elf_ws.distance_transform_watershed(boundaries, **ws_kwargs)[0]


def _compute_features(raw, boundaries, watershed, feature_names, use_2dws, n_threads):
    if (len(FEATURE_NAMES - set(feature_names)) > 0) or (len(feature_names) == 0):
        raise ValueError("Invalid feature set")
    if raw.shape != boundaries.shape:
        raise ValueError("Shapes %s and %s do not match" % (str(raw.shape), str(boundaries.shape)))
    if raw.shape != watershed.shape:
        raise ValueError("Shapes %s and %s do not match" % (str(raw.shape), str(watershed.shape)))

    rag = elf_feats.compute_rag(watershed, n_threads=n_threads)

    features = []
    if 'raw-edge-features' in feature_names:
        feats = elf_feats.compute_boundary_features_with_filters(rag, raw, use_2dws,
                                                                 n_threads=n_threads)
        features.append(feats)
    if 'boundary-edge-features' in feature_names:
        feats = elf_feats.compute_boundary_features_with_filters(rag, boundaries, use_2dws,
                                                                 n_threads=n_threads)
        features.append(feats)
    if 'raw-region-features' in feature_names:
        feats = elf_feats.compute_region_features(rag.uvIds(), raw, watershed,
                                                  n_threads=n_threads)
        features.append(feats)

    # for now, we always append the length as one other feature
    # eventually, it would be nice to add topolgy features, cf.
    # https://github.com/ilastik/nature_methods_multicut_pipeline/blob/master/software/multicut_src/DataSet.py#L954
    # https://github.com/DerThorsten/nifty/blob/master/src/python/lib/graph/rag/accumulate.cxx#L361
    edge_len = elf_feats.compute_boundary_mean_and_length(rag, raw, n_threads=n_threads)[:, 1]
    features.append(edge_len[:, None])

    features = np.concatenate(features, axis=1)
    assert len(features) == rag.numberOfEdges
    return rag, features


def _compute_features_and_labels(raw, boundaries, watershed, labels,
                                 use_2dws, feature_names, ignore_label, n_threads):
    rag, features = _compute_features(raw, boundaries, watershed, feature_names, use_2dws, n_threads)

    if ignore_label is None:
        edge_labels = elf_learn.compute_edge_labels(rag, labels, n_threads=n_threads)
    else:
        edge_labels, edge_mask = elf_learn.compute_edge_labels(rag, labels, ignore_label, n_threads)
        features, edge_labels = features[edge_mask], edge_labels[edge_mask]

    if use_2dws:
        z_edges = elf_feats.compute_z_edge_mask(rag, watershed)
    else:
        z_edges = None

    return features, edge_labels, z_edges


def edge_training(raw, boundaries, labels, use_2dws, watershed=None,
                  feature_names=FEATURE_NAMES, ws_kwargs=DEFAULT_WS_KWARGS,
                  learning_kwargs=DEFAULT_RF_KWARGS, n_threads=None):
    """ Train random forest classifier for edges.

    Arguments:
        raw [np.ndarray] -
    """

    rf_kwargs = deepcopy(learning_kwargs)
    ignore_label = rf_kwargs.pop('ignore_label', None)

    if isinstance(raw, np.ndarray):
        if (not isinstance(boundaries, np.ndarray)) or (not isinstance(labels, np.ndarray)):
            raise ValueError("Expect raw data, boundaries and labels to be either all numpy arrays or lists")

        if watershed is None:
            watershed = _compute_watershed(boundaries, use_2dws, ws_kwargs, n_threads)
        features, edge_labels, z_edges = _compute_features_and_labels(raw, boundaries, watershed, labels,
                                                                      use_2dws, feature_names,
                                                                      ignore_label, n_threads)
    else:
        if not (len(raw) == len(boundaries) == len(labels)):
            raise ValueError("Expect same number of raw data, boundary and label arrays")
        if watershed is not None and len(watershed) != len(raw):
            raise ValueError("Expect same number of watershed arrays as raw data")

        features = []
        edge_labels = []
        z_edges = []
        for train_id, (this_raw, this_boundaries, this_labels) in enumerate(zip(raw, boundaries, labels)):
            if watershed is None:
                this_watershed = _compute_watershed(this_boundaries, use_2dws, ws_kwargs, n_threads)
            else:
                this_watershed = watershed[train_id]

            this_features, this_edge_labels, this_z_edges = _compute_features_and_labels(this_raw, this_boundaries,
                                                                                         this_watershed, this_labels,
                                                                                         use_2dws, feature_names,
                                                                                         ignore_label, n_threads)
            features.append(this_features)
            edge_labels.append(this_edge_labels)
            if use_2dws:
                assert this_z_edges is not None
                z_edges.append(this_z_edges)

        features = np.concatenate(features, axis=0)
        edge_labels = np.concatenate(edge_labels, axis=0)
        if use_2dws:
            z_edges = np.concatenate(z_edges, axis=0)

    assert len(features) == len(edge_labels), "%i, %i" % (len(features),
                                                          len(edge_labels))

    if use_2dws:
        assert len(features) == len(z_edges)
        rf = elf_learn.learn_random_forests_for_xyz_edges(features, edge_labels, z_edges, n_threads=n_threads,
                                                          **rf_kwargs)
    else:
        rf = elf_learn.learn_edge_random_forest(features, edge_labels, n_threads=n_threads, **rf_kwargs)
    return rf


def multicut_segmentation(raw, boundaries, rf,
                          use_2dws, multicut_solver, watershed=None,
                          feature_names=FEATURE_NAMES, weighting_scheme=None,
                          ws_kwargs=DEFAULT_WS_KWARGS, solver_kwargs={},
                          beta=0.5, n_threads=None, return_intermediates=False):
    """ Instance segmentation with multicut with edge costs
    derived from random forest predictions.

    Arguments:
        raw [np.ndarray] -
    """

    if isinstance(multicut_solver, str):
        solver = elf_mc.get_multicut_solver(multicut_solver)
    else:
        if not callable(multicut_solver):
            raise ValueError("Invalid multicut solver")
        solver = multicut_solver

    if watershed is None:
        watershed = _compute_watershed(boundaries, use_2dws, ws_kwargs, n_threads)

    rag, features = _compute_features(raw, boundaries, watershed,
                                      feature_names, use_2dws, n_threads)

    if use_2dws:
        rf_xy, rf_z = rf
        z_edges = elf_feats.compute_z_edge_mask(rag, watershed)
        edge_probs = elf_learn.predict_edge_random_forests_for_xyz_edges(rf_xy, rf_z,
                                                                         features, z_edges, n_threads)
    else:
        edge_probs = elf_learn.predict_edge_random_forest(rf, features, n_threads)
        z_edges = None

    edge_sizes = elf_feats.compute_boundary_mean_and_length(rag, raw, n_threads)[:, 1]
    costs = elf_mc.compute_edge_costs(edge_probs, edge_sizes=edge_sizes, beta=beta,
                                      z_edge_mask=z_edges, weighting_scheme=weighting_scheme)

    # we pass the watershed to the solver as well, because it is needed for
    # the blockwise-multicut solver
    node_labels = solver(rag, costs, n_threads=n_threads,
                         segmentation=watershed, **solver_kwargs)
    seg = elf_feats.project_node_labels_to_pixels(rag, node_labels, n_threads)

    if return_intermediates:
        return {'watershed': watershed,
                'rag': rag,
                'features': features,
                'costs': costs,
                'node_labels': node_labels,
                'segmentation': seg}
    else:
        return seg


def multicut_workflow(train_raw, train_boundaries, train_labels,
                      boundaries, raw, use_2dws, multicut_solver,
                      train_watershed=None, watershed=None,
                      feature_names=FEATURE_NAMES, weighting_scheme=None,
                      ws_kwargs=DEFAULT_WS_KWARGS, learning_kwargs=DEFAULT_RF_KWARGS,
                      solver_kwargs={}, beta=0.5, n_threads=None):
    """ Run workflow for multicut segmentation based on boundary maps with edge weights learned via random forest.

    Based on "Multicut brings automated neurite segmentation closer to human performance":
    https://hci.iwr.uni-heidelberg.de/sites/default/files/publications/files/217205318/beier_17_multicut.pdf

    Arguments:
        train_raw [np.ndarray or list[np.ndarray]] -
        train_boundaries [np.ndarray or list[np.ndarray]] -
        train_labels [np.ndarray or list[np.ndarray]] -
        boundaries [np.ndarray] -
        raw [np.ndarray] -
        use_2dws [bool] -
        multicut_solver [str or callable] -
        train_watershed [np.ndarray] -
        watershed [np.ndarray] -
        feature_names [listlike] -
        weighting_scheme [str] -
        ws_kwargs -
        rf_kwargs -
        solver_kwargs -
        beta -
        n_threads [int] -
    """
    rf = edge_training(train_raw, train_boundaries, train_labels, use_2dws,
                       watershed=train_watershed, feature_names=feature_names,
                       ws_kwargs=ws_kwargs, learning_kwargs=learning_kwargs,
                       n_threads=n_threads)

    seg = multicut_segmentation(raw, boundaries, rf,
                                use_2dws, multicut_solver,
                                watershed=watershed, feature_names=feature_names,
                                weighting_scheme=weighting_scheme, ws_kwargs=ws_kwargs,
                                solver_kwargs=solver_kwargs, beta=beta, n_threads=n_threads)
    return seg


# ref: https://github.com/constantinpape/mu-net/blob/master/mu_net/utils/segmentation.py#L157
def simple_multicut_workflow(input_, watershed=None, use_2dws=False,
                             ws_kwargs=DEFAULT_WS_KWARGS, n_threads=None):
    """ Run simplified multicut segmentation workflow from affinity or boundary maps.

    Adapted from "Multicut brings automated neurite segmentation closer to human performance":
    https://hci.iwr.uni-heidelberg.de/sites/default/files/publications/files/217205318/beier_17_multicut.pdf

    Arguments:
        input_ [np.ndarray] -
        watershed [np.ndarray] -
        use_2dws [bool] -
        ws_kwargs [dict] -
        n_threads [int] -
    """
