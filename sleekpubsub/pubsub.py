import redis
import threading
import uuid
import json
import nodes
from consts import *
from exceptions import *

class ACL(object):
    def can_publish(self, ident, node, item, id):
        return True

    def can_create(self, ident, node, config):
        return True

    def can_delete(self, ident, node, config):
        return True

    def can_retract(self, ident, node, item):
        return True

    def subscribe(self, ident, node, id):
        return True

class ConfigCache(object):
    def __init__(self, pubsub):
        self._nodes = {}
        self.pubsub = pubsub
        self.lock = threading.Lock()
        self.instance = uuid.uuid4().hex

    def __getitem__(self, node):
        with self.lock:
            if node in self._nodes:
                return self._nodes[node]
            else:
                if not self.pubsub.node_exists(node):
                    raise NodeDoesNotExist
                config = json.loads(self.pubsub.redis.get(NODECONFIG % node))
                self._nodes[node] = self.pubsub.nodetypes[config.get(u'type', u'leaf')](self.pubsub, node, config)
                return self._nodes[node]

    def invalidate(self, data, delete=False):
        node, instance = data.split("\x00")
        if instance != self.instance:
            with self.lock:
                if node in self._nodes:
                    if delete:
                        del self._nodes[node]
                    else:
                        del self._nodes[node].config

class Pubsub(object):
    def __init__(self, allnodes=True, host='localhost', port=6379, db=0, listen=False):
        self.allnodes = allnodes
        self.host = host
        self.port = port
        self.db = db
        self.redis = redis.Redis(host=self.host, port=self.port, db=self.db)
        self.lredis = None
        self.interface = {}
        self.nodetypes = {}
        self.nodes = set()
        self.nodeconfig = ConfigCache(self)
        self.listen_ready = threading.Event()
        
        self.register_nodetype(u'leaf', nodes.Leaf)
        self.register_nodetype(u'queue', nodes.Queue)
        self.register_nodetype(u'job', nodes.Job)

        if listen:
            #start listener thread
            self.lthread = threading.Thread(target=self.listen)
            self.lthread.daemon = True
            self.lthread.start()
            self.listen_ready.wait()

    def __getitem__(self, node):
        return self.nodeconfig[node]

    def register_nodetype(self, nodetype, klass):
        self.nodetypes[nodetype] = klass
        def startclass(node, config={}):
            if self.node_exists(node):
                return self[node]
            else:
                if not config.get('type', False):
                    config['type'] = nodetype
                return self.create_node(node, config, True)
        setattr(self, nodetype, startclass)

    def register_interface(self, interface):
        self.interface[interface.name] = interface
        interface.register(self)

    def create_node(self, node, config, returnnode=False):
        if not self.redis.sadd("nodes", node):
            raise NodeExists
        self.nodes.add(node)
        self.set_node_config(node, config)
        self.redis.publish(NEWNODE, node)
        if returnnode:
            return self[node]

    def set_node_config (self, node, config):
        if not self.node_exists(node):
            raise NodeDoesNotExist
        if type(config) == dict:
            if u'type' not in config:
                config[u'type'] = u'leaf'
            jconfig = json.dumps(config)
            dconfig = config
        else:
            dconfig = json.loads(config)
            if u'type' not in dconfig:
                dconfig[u'type'] = u'leaf'
            jconfig = json.dumps(dconfig)
        self.redis.set(NODECONFIG % node, jconfig)
        self.redis.publish(CONFNODE, "%s\x00%s" % (node, self.nodeconfig.instance))

    def get_nodes(self):
        return self.nodes
    
    def node_exists(self, node, check=False):
        return self.redis.sismember('nodes', node)
            #return node in self.nodes
        #extra sure way.... worth it?

    def close(self):
        self.redis.connection.disconnect()
        self.lredis.connection.disconnect()

    def listen(self):
        #listener redis object
        self.lredis = redis.Redis(host=self.host, port=self.port, db=self.db)

        # subscribe to node activities channel
        self.lredis.subscribe((NEWNODE, DELNODE, CONFNODE))

        #get set of nodes
        self.nodes.update(self.redis.smembers('nodes'))
        if self.nodes:
            # subscribe to exist nodes retract and publish
            for node in self.nodes:
                self.lredis.subscribe(self[node].get_channels())

        self.listen_ready.set()
        for event in self.lredis.listen():
            if event['type'] == 'message':
                if event['channel'].startswith('node.publish'):
                    #node publish event
                    id, item = event['data'].split('\x00', 1)
                    self.publish_notice(event['channel'].split(':', 1)[-1], item, id)
                elif event['channel'].startswith('node.retract'):
                    self.retract_notice(event['channel'].split(':', 1)[-1], event['data'])
                elif event['channel'] == NEWNODE:
                    #node created event
                    self.nodes.add(event['data'])
                    n = self[event['data']]
                    self.lredis.subscribe(n.get_channels())
                    self.create_notice(event['data'])
                elif event['channel'] == DELNODE:
                    #node destroyed event
                    try:
                        self.nodes.remove(event['data'])
                    except KeyError:
                        #already removed -- probably locally
                        pass
                    self.lredis.unsubscribe(self[event['data']].get_channels())
                    self.nodeconfig.invalidate(event['data'], delete=True)
                    self.delete_notice(event['data'])
                elif event['channel'] == CONFNODE:
                    self.nodeconfig.invalidate(event['data'])
                elif event['channel'].startswith("node.finished"):
                    node = event['channel'].split(":", -1)[-1]
                    id, item, result = event['data'].split('\x00', 3)
                    self.finish_notice(node, id, item, result)

    
    def create_notice(self, node):
        for ifname in self.interface:
            self.interface[ifname].create_notice(node)

    def delete_notice(self, node):
        for ifname in self.interface:
            self.interface[ifname].delete_notice(node)

    def publish_notice(self, node, item, id):
        self[node].event_publish(id, item)
        for ifname in self.interface:
            self.interface[ifname].publish_notice(node, item, id)

    def retract_notice(self, node, id):
        self[node].event_retract(id)
        for ifname in self.interface:
            self.interface[ifname].retract_notice(node, id)

    def finish_notice(self, node, id, item, result):
        self[node].event_finished(id, item, result)
        for ifname in self.interface:
            self.interface[ifname].finish_notice(node, id, item, result)

class Interface(object):
    name = None
    def __init__(self):
        self.pubsub = None
        self.acl = None

    def register(self, pubsub):
        self.pubsub = pubsub
        self.start()

    def start(self):
        """This should be overridden"""
        self.acl = ACL()

    def publish_notice(self, node, item, id):
        pass

    def retract_notice(self, node, id):
        pass

    def create_notice(self, node):
        pass

    def delete_notice(self, node):
        pass
    
    def finish_notice(self, node, id, item, result):
        pass
