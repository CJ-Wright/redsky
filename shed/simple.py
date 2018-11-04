"""Translation nodes"""
import time
import uuid
from collections import MutableMapping
from collections import deque

import networkx as nx
import numpy as np
from shed.doc_gen import CreateDocs
from regolith.chained_db import ChainDB, _convert_to_dict
from rapidz.clients import result_maybe
from rapidz.core import Stream, buffer, zip as szip, move_to_first
from rapidz.parallel import ParallelStream

ALL = "--ALL THE DOCS--"

DTYPE_MAP = {np.ndarray: "array", int: "number", float: "number"}


def _hash_or_uid(node):
    return getattr(node, "uid", hash(node))


def walk_to_translation(node, graph, prior_node=None):
    """Creates a graph that is a subset of the graph from the stream.

    The walk starts at a translation ``ToEventStream`` node and ends at any
    instances of FromEventStream or ToEventStream.  Each iteration of the walk
    goes up one node, determines if that node is a ``FromEventStream`` node, if
    not walks one down to see if there are any ``ToEventStream`` nodes, if not
    it keeps walking up. The walk down allows us to replace live data with
    stored data/stubs when it comes time to get the parent uids. Graph nodes
    are hashes or uids of the node objects with ``stream=node`` in the nodes.

    Parameters
    ----------
    node : Stream instance
    graph : DiGraph instance
    prior_node : Stream instance
    """
    if node is None:
        return
    t = _hash_or_uid(node)
    graph.add_node(t, stream=node)
    if prior_node:
        tt = _hash_or_uid(prior_node)
        if graph.has_edge(t, tt):
            return
        else:
            graph.add_edge(t, tt)
            if isinstance(node, SimpleFromEventStream):
                return
            else:
                for downstream in node.downstreams:
                    ttt = _hash_or_uid(downstream)
                    if (
                        isinstance(downstream, SimpleToEventStream)
                        and ttt not in graph
                    ):
                        graph.add_node(ttt, stream=downstream)
                        graph.add_edge(t, ttt)
                        return

    for node2 in node.upstreams:
        # Stop at translation node
        if node2 is not None:
            walk_to_translation(node2, graph, node)


@Stream.register_api()
class SimpleFromEventStream(Stream):
    """Extracts data from the event stream, and passes it downstream.

    Parameters
    ----------

    doc_type : {'start', 'descriptor', 'event', 'stop'}
        The type of document to extract data from
    data_address : tuple
        A tuple of successive keys walking through the document considered,
        if the tuple is empty all the data from that document is returned as
        a dict
    upstream : Stream instance or None, optional
        The upstream node to receive streams from, defaults to None
    event_stream_name : str, optional
        Filter by en event stream name (see :
        http://nsls-ii.github.io/databroker/api.html?highlight=stream_name#data)
    stream_name : str, optional
        Name for this stream node
    principle : bool, optional
        If True then when this node receives a stop document then all
        downstream ToEventStream nodes will issue a stop document.
        Defaults to False. Note that one principle node is required for proper
        pipeline operation.

    Notes
    -----
    The result emitted from this stream no longer follows the document model.

    This node also keeps track of when and which data came through the node.


    Examples
    -------------
    import uuid
    from shed.event_streams import EventStream
    from shed.translation import FromEventStream

    s = EventStream()
    s2 = FromEventStream(s, 'event', ('data', 'det_image'))
    s3 = s2.map(print)
    s.emit(('start', {'uid' : str(uuid.uuid4())}))
    s.emit(('descriptor', {'uid' : str(uuid.uuid4())}))
    s.emit(('event', {'uid' : str(uuid.uuid4()), 'data': {'det_image' : 1}}))
    s.emit(('stop', {'uid' : str(uuid.uuid4())}))
    prints:
    1
    """

    def __init__(
        self,
        doc_type,
        data_address,
        upstream=None,
        event_stream_name=ALL,
        stream_name=None,
        principle=False,
        **kwargs
    ):
        asynchronous = None
        if "asynchronous" in kwargs:
            asynchronous = kwargs.pop("asynchronous")
        Stream.__init__(
            self, upstream, stream_name=stream_name, asynchronous=asynchronous
        )
        self.principle = principle
        self.doc_type = doc_type
        if isinstance(data_address, str):
            data_address = tuple([data_address])
        self.data_address = data_address
        self.event_stream_name = event_stream_name
        self.uid = str(uuid.uuid4())
        self.descriptor_uids = []
        self.subs = []
        self.start_uid = None

    def update(self, x, who=None):
        name, doc = x
        if name == "start":
            self.start_uid = doc["uid"]
            # Sideband start document in
            [s.emit_start(x) for s in self.subs]
        if name == "descriptor" and (
            self.event_stream_name == ALL
            or self.event_stream_name == doc.get("name", "primary")
        ):
            self.descriptor_uids.append(doc["uid"])
        if name == "stop":
            # Trigger the downstream nodes to make a stop but they can emit
            # on their own time
            self.descriptor_uids = []
            [s.emit_stop(x) for s in self.subs]
        inner = doc.copy()
        if name == self.doc_type and (
            (
                name == "descriptor"
                and (self.event_stream_name == doc.get("name", ALL))
            )
            or (
                name == "event" and (doc["descriptor"] in self.descriptor_uids)
            )
            or name in ["start", "stop"]
        ):

            # If we have an empty address get everything
            if self.data_address != ():
                for da in self.data_address:
                    # If it's a tuple we want multiple things at once
                    if isinstance(da, tuple):
                        inner = tuple(inner[daa] for daa in da)
                    else:
                        if da in inner:
                            inner = inner[da]
                        else:
                            return
            return self.emit(inner)


@Stream.register_api()
class SimpleToEventStream(Stream, CreateDocs):
    """Converts data into a event stream, and passes it downstream.

    Parameters
    ----------
    upstream :
        the upstream node to receive streams from
    data_keys: tuple, optional
        Names of the data keys. If None assume incoming data is dict and use
        the keys from the dict. Defauls to None
    stream_name : str, optional
        Name for this stream node

    Notes
    -----
    The result emitted from this stream follows the document model.
    This is essentially a state machine. Transitions are:
    start -> stop
    start -> descriptor -> event -> stop
    Note that start -> start is not allowed, this node always issues a stop
    document so the data input times can be stored.

    Examples
    --------
    import uuid
    from shed.event_streams import EventStream
    from shed.translation import FromEventStream, ToEventStream

    s = EventStream()
    s2 = FromEventStream(s, 'event', ('data', 'det_image'), principle=True)
    s3 = ToEventStream(s2, ('det_image',))
    s3.sink(print)
    s.emit(('start', {'uid' : str(uuid.uuid4())}))
    s.emit(('descriptor', {'uid' : str(uuid.uuid4()),
                           'data_keys': {'det_image': {'units': 'arb'}}))
    s.emit(('event', {'uid' : str(uuid.uuid4()), 'data': {'det_image' : 1}}))
    s.emit(('stop', {'uid' : str(uuid.uuid4())}))
    prints:
    ('start',...)
    ('descriptor',...)
    ('event',...)
    ('stop',...)
    """

    def __init__(self, upstream, data_keys=None, stream_name=None, **kwargs):
        if stream_name is None:
            stream_name = str(data_keys)

        Stream.__init__(self, upstream, stream_name=stream_name)
        CreateDocs.__init__(self, data_keys, **kwargs)

        move_to_first(self)

        self.start_document = None

        self.state = "stopped"
        self.subs = []

        self.uid = str(uuid.uuid4())

        # walk upstream to get all upstream nodes to the translation node
        # get start_uids from the translation node
        self.graph = nx.DiGraph()
        walk_to_translation(self, graph=self.graph)

        self.translation_nodes = {
            k: n["stream"]
            for k, n in self.graph.node.items()
            if isinstance(
                n["stream"], (SimpleFromEventStream, SimpleToEventStream)
            )
            and n["stream"] != self
        }
        self.principle_nodes = [
            n
            for k, n in self.translation_nodes.items()
            if getattr(n, "principle", False)
            or isinstance(n, SimpleToEventStream)
        ]
        if not self.principle_nodes:
            raise RuntimeError("No Principle Nodes Detected")
        for p in self.principle_nodes:
            p.subs.append(self)

    def emit_start(self, x):
        # Emergency stop
        if self.state != "stopped":
            self.emit_stop(x)
        start = self.create_doc("start", x)
        self.emit(start)
        [s.emit_start(x) for s in self.subs]
        self.state = "started"
        self.start_document = None

    def emit_stop(self, x):
        stop = self.create_doc("stop", x)
        ret = self.emit(stop)
        [s.emit_stop(x) for s in self.subs]
        self.state = "stopped"
        return ret

    def update(self, x, who=None):
        rl = []
        # If we have a start document ready to go, release it.
        if self.state == "started":
            rl.append(self.emit(self.create_doc("descriptor", x)))
            self.state = "described"
        rl.append(self.emit(self.create_doc("event", x)))

        return rl

    def start_doc(self, x):
        new_start_doc = super().start_doc(x)
        new_start_doc.update(
            dict(
                parent_uids=[
                    v.start_uid
                    for k, v in self.translation_nodes.items()
                    if v.start_uid is not None
                ],
                parent_node_map={
                    v.uid: v.start_uid
                    for k, v in self.translation_nodes.items()
                    if v.start_uid is not None
                },
            )
        )
        self.start_document = ("start", new_start_doc)
        return new_start_doc


@Stream.register_api()
class AlignEventStreams(szip):
    """Zips and aligns multiple streams of documents, note that the last
    upstream takes precedence where merging is not possible, this requires
    the two streams to be of equal length."""

    def __init__(self, *upstreams, stream_name=None, **kwargs):
        szip.__init__(self, *upstreams, stream_name=stream_name)
        doc_names = ["start", "descriptor", "event", "stop"]
        self.true_buffers = {
            k: {
                upstream: deque()
                for upstream in upstreams
                if isinstance(upstream, Stream)
            }
            for k in doc_names
        }
        self.true_literals = {
            k: [
                (i, val)
                for i, val in enumerate(upstreams)
                if not isinstance(val, Stream)
            ]
            for k in doc_names
        }

    def _emit(self, x):
        # flatten out the nested setup
        x = [k for l in x for k in l]
        names = x[::2]
        docs = x[1::2]
        super()._emit((names[0], _convert_to_dict(ChainDB(*docs))))

    def update(self, x, who=None):
        name, doc = x
        self.buffers = self.true_buffers[name]
        self.literals = self.true_literals[name]
        super().update((name, doc), who)
