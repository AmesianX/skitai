import time
from skitai.server.threads import socket_map
from skitai.server.threads import trigger
from aquests.client.asynconnect import AsynSSLConnect
import threading
from aquests.protocols.http import request as http_request
from aquests.protocols.http import request_handler as http_request_handler
from aquests.protocols.http2 import request_handler as http2_request_handler
from aquests.protocols.grpc.request import GRPCRequest
from aquests.protocols.http import response as http_response
from aquests.protocols.ws import request_handler as ws_request_handler
from aquests.protocols.ws import request as ws_request
from . import rcache

class OperationError (Exception):
	pass

class Result (rcache.Result):
	def __init__ (self, id, status, response, ident = None):
		rcache.Result.__init__ (self, status, ident)		
		self.node = id
		self._response = response
		try:
			self.set_result ()
		except:
			self.status, self.code, self.msg = 2, 720, "Result Error"
		
	def __getattr__ (self, attr):
		return getattr (self._response, attr)
					
	def set_result (self):
		self.code = self._response.code
		self.msg = self._response.msg
		self.data = self._response.data
	
	def reraise (self):
		if self.status != 3:
			raise OperationError ("%d %s" % (self.code, self.msg))
	
	def get_error_as_string (self):
		return "<OperationError> %d %s" % (self.code, self.msg)		
				
	def cache (self, timeout = 300):
		self._response = None
		if self.status != 3:
			return
		rcache.Result.cache (self, timeout)
		

class Results (rcache.Result):
	def __init__ (self, results, ident = None):
		self.results = results
		self.code = [rs.code for rs in results]
		rcache.Result.__init__ (self, [rs.status for rs in self.results], ident)
		
	def __iter__ (self):
		return self.results.__iter__ ()
	
	def cache (self, timeout = 300):
		if self.is_cached:
			return
		if rcache.the_rcache is None or not self.ident: 
			return
		if [_f for _f in [rs.status != 3 for rs in self.results] if _f]:
			return				
		rcache.Result.__timeout = timeout
		rcache.Result.__cached_time = time.time ()
		
		rcache.the_rcache.cache (self)
		
			
class Dispatcher:
	def __init__ (self, cv, id, ident = None, filterfunc = None, cachefs = None):
		self._cv = cv
		self.id = id
		self.ident = ident
		self.filterfunc = filterfunc
		self.cachefs = cachefs
		self.creation_time = time.time ()
		self.status = 0		
		self.result = None
		self.handler = None
			
	def get_id (self):
		return self.id
	
	def get_status (self):
		# 0: Not Connected
		# 1: Operation Timeout
		# 2: Exception Occured
		# 3: Normal
		self._cv.acquire ()		
		status = self.status
		self._cv.release ()
		return status
		
	def set_status (self, code):
		self._cv.acquire ()
		self.status = code
		self._cv.notify ()
		self._cv.release ()
		return code
		
	def get_result (self):
		if self.result is None: # timeout
			if self.get_status () == -1:
				self.result = Result (self.id, -1, http_response.FailedResponse (731, "Request Failed"), self.ident)
			else:	
				self.result = Result (self.id, 1, http_response.FailedResponse (730, "Timeout"), self.ident)
		return self.result
	
	def do_filter (self):
		if self.filterfunc:
			self.filterfunc (self.result)
			
	def handle_cache (self, response):
		status = 3
		self.result = Result (self.id, status, response, self.ident)				
		self.set_status (status)
							
	def handle_result (self, handler):
		if self.get_status () == 1:
			# timeout, ignore
			return
	
		response = handler.response		
		# DON'T do_filter here, it blocks select loop		
		if response.code >= 700:
			status = 2
		else:
			status = 3
		
		self.result = Result (self.id, status, response, self.ident)
		self.set_status (status)
		
		cakey = response.request.get_cache_key ()
		if self.cachefs and cakey and response.max_age:
			self.cachefs.save (
				cakey,
				response.get_header ("content-type"), response.content, 
				response.max_age, 0
			)

		handler.asyncon = None
		handler.callback = None
		handler.response = None
		

class ClusterDistCall:
	def __init__ (self,
		cluster, 
		uri,
		params = None,
		reqtype = "get",
		headers = None,
		auth = None,
		encoding = None,	
		use_cache = False,	
		mapreduce = True,
		filter = None,
		callback = None,
		cachefs = None,
		logger = None
		):
		
		self._cluster = cluster
		self._uri = uri
		self._params = params
		self._headers = headers
		self._reqtype = reqtype
			
		self._auth = auth
		self._encoding = encoding
		self._use_cache = use_cache
		self._mapreduce = mapreduce
		self._filter = filter
		self._callback = callback
		self._cachefs = cachefs
		self._logger = logger
	
		self._requests = {}
		self._results = []
		self._canceled = 0
		self._init_time = time.time ()
		self._cv = None
		self._retry = 0		
		self._cached_request_args = None		
		self._numnodes = 0
		self._cached_result = None
		
		if self._cluster:
			nodes = self._cluster.get_nodes ()
			self._numnodes = len (nodes)
			if self._mapreduce:
				self._nodes = nodes
			else: # anyone of nodes
				self._nodes = [None]
		
		if not self._reqtype.lower ().endswith ("rpc"):
			self._request ("", self._params)
	
	def __del__ (self):
		self._cv = None
		self._results = []
	
	def _get_ident (self):
		cluster_name = self._cluster.get_name ()
		if cluster_name == "socketpool":
			_id = "%s/%s" % (self._uri, self._reqtype)
		else:
			_id = "%s/%s/%s" % (cluster_name, self._uri, self._reqtype)		
		_id += "/%s/%s" % self._cached_request_args
		_id += "%s" % (
			self._mapreduce and "/M" or ""			
			)
		return _id
	
	def _add_header (self, n, v):
		if self._headers is None:
			self._headers = {}
		self._headers [n] = v
	
	_TYPEMAP = [
		("form", "application/x-www-form-urlencoded"),
		("xml", "text/xml"),	
		("nvp", "text/namevalue")		
	]
	def _map_content_type (self, _reqtype):
		for alias, ct in self._TYPEMAP:
			if _reqtype.endswith (alias):
				self._add_header ("Content-Type", ct)
				return _reqtype [:-len (alias)]
		return _reqtype
	
	def _handle_request (self, request, rs, asyncon, handler):
		if self._cachefs:
			# IMP: mannual address setting
			request.set_address (asyncon.address)
			cakey = request.get_cache_key ()
			if cakey:			
				cachable = self._cachefs.is_cachable (
					request.get_header ("cache-control"),
					request.get_header ("cookie") is not None, 
					request.get_header ("authorization") is not None,
					request.get_header ("pragma")
				)
				
				if cachable:
					hit, compressed, max_age, content_type, content = self._cachefs.get (cakey, undecompressible = 0)			
					if hit:
						header = "HTTP/1.1 200 OK\r\nContent-Type: %s\r\nX-Skitaid-Cache-Lookup: %s" % (
							content_type, hit == 1 and "MEM_HIT" or "HIT"
						)		
						response = http_response.Response (request, header)
						response.collect_incoming_data (content)
						response.done ()
						asyncon.set_active (False)
						rs.handle_cache (response)						
						return 0
		
		r = handler (asyncon, request, self._callback and self._callback or rs.handle_result)		
		if asyncon.get_proto () and asyncon.isconnected ():
			asyncon.handler.handle_request (r)
		else:				
			r.handle_request ()
		
		return 1
						
	def _request (self, method, params):
		self._cached_request_args = (method, params) # backup for retry
		if self._use_cache and rcache.the_rcache:
			self._cached_result = rcache.the_rcache.get (self._get_ident ())
			if self._cached_result is not None:
				return
		
		requests = 0
		while self._avails ():
			if self._cluster.get_name () != "__socketpool__":
				asyncon = self._get_connection (None)
			else:
				asyncon = self._get_connection (self._uri)
			
			_reqtype = self._reqtype.lower ()
			rs = Dispatcher (self._cv, asyncon.address, ident = not self._mapreduce and self._get_ident () or None, filterfunc = self._filter, cachefs = self._cachefs)
			self._requests[rs] = asyncon
			
			try:
				if _reqtype in ("ws", "wss"):
					handler = ws_request_handler.RequestHandler					
					request = ws_request.Request (self._uri, params, self._headers, self._encoding, self._auth, self._logger)
												
				else:				
					if not self._use_cache:
						self._add_header ("Cache-Control", "no-cache")				
					
					handler = http_request_handler.RequestHandler					
					if _reqtype == "rpc":
						request = http_request.XMLRPCRequest (self._uri, method, params, self._headers, self._encoding, self._auth, self._logger)				
					elif _reqtype == "grpc":
						request = GRPCRequest (self._uri, method, params, self._headers, self._encoding, self._auth, self._logger)						
					elif _reqtype == "upload":
						request = http_request.HTTPMultipartRequest (self._uri, _reqtype, params, self._headers, self._encoding, self._auth, self._logger)
					else:
						if params:
							_reqtype = self._map_content_type (_reqtype)
						request = http_request.HTTPRequest (self._uri, _reqtype, params, self._headers, self._encoding, self._auth, self._logger)				
				
				requests += self._handle_request (request, rs, asyncon, handler)
					
			except:
				self._logger ("Request Creating Failed", "fail")
				self._logger.trace ()
				rs.set_status (-1)
				asyncon.set_active (False)
				continue
			
		if requests:
			trigger.wakeup ()
		
		if _reqtype [-3:] == "rpc":
			return self
			
	def _avails (self):
		return len (self._nodes)
	
	def _get_connection (self, id = None):
		if id is None: id = self._nodes.pop ()
		else: self._nodes = []
		asyncon = self._cluster.get (id)
		if self._cv is None:
			self._cv = asyncon._cv
		return asyncon
			
	def _cancel (self):
		self._canceled = 1
	
	def cache (self, timeout = 300):
		if self._cached_result is None:
			raise ValueError("call getwait, getswait first")
		self._cached_result.cache (timeout)	
			
	def wait (self, timeout = 3, reraise = True):
		self.getswait (timeout, reraise)
		self._cached_result = None
		
	def getwait (self, timeout = 3, reraise = False):
		if self._cached_result is not None:
			return self._cached_result
			
		self._wait (timeout)
		if len (self._results) > 1:
			raise ValueError("Multiple Results, Use getswait")
		self._cached_result = self._results [0].get_result ()
		if reraise:
			self._cached_result.reraise ()
		return self._cached_result
	
	def getswait (self, timeout = 3, reraise = False):
		if self._cached_result is not None:
			return self._cached_result
			
		self._wait (timeout)
		rss = [rs.get_result () for rs in self._results]
		if reraise:
			[rs.reraise () for rs in rss]
		self._cached_result = Results (rss, ident = self._get_ident ())
		return self._cached_result
	
	def _collect_result (self):
		for rs, asyncon in list(self._requests.items ()):
			status = rs.get_status ()			
			if status == -1:
				del self._requests [rs]
				self._results.append (rs)
				self._cluster.report (asyncon, True) # not asyncons' Fault				
			
			elif not self._mapreduce and status == 2 and self._retry < (self._numnodes - 1):
				self._logger ("Cluster Response Error, Switch To Another...", "fail")
				self._cluster.report (asyncon, False) # exception occured
				del self._requests [rs]
				self._retry += 1
				self._nodes = [None]
				self._request (*self._cached_request_args)
				
			elif status >= 2:
				del self._requests [rs]
				self._results.append (rs)
				if status == 2:
					self._cluster.report (asyncon, False) # exception occured
				else:	
					self._cluster.report (asyncon, True) # well-functioning
					rs.do_filter ()
					
	def _wait (self, timeout = 3):
		self._collect_result ()
		while self._requests and not self._canceled:
			remain = timeout - (time.time () - self._init_time)
			if remain <= 0: break						
			self._cv.acquire ()
			self._cv.wait (remain)
			self._cv.release ()
			self._collect_result ()
		
		# timeouts	
		for rs, asyncon in list(self._requests.items ()):
			asyncon.handle_abort () # abort imme
			rs.set_status (1)
			self._cluster.report (asyncon, False) # maybe dead
			self._results.append (rs)
			del self._requests [rs]

#-----------------------------------------------------------
# Cluster Base Call
#-----------------------------------------------------------
class _Method:
	def __init__(self, send, name):
		self.__send = send
		self.__name = name
		
	def __getattr__(self, name):
		return _Method(self.__send, "%s.%s" % (self.__name, name))
		
	def __call__(self, *args):
		return self.__send(self.__name, args)

		
class Proxy:
	def __init__ (self, __class, *args, **kargs):
		self.__class = __class
		self.__args = args
		self.__kargs = kargs		
	
	def __getattr__ (self, name):	  
		return _Method(self.__request, name)
	
	def __request (self, method, params):		
		cdc = self.__class (*self.__args, **self.__kargs)
		cdc._request (method, params)
		return cdc

	
class ClusterDistCallCreator:
	def __init__ (self, cluster, logger, cachesfs):
		self.cluster = cluster				
		self.logger = logger
		self.cachesfs = cachesfs		
	
	def __getattr__ (self, name):	
		return getattr (self.cluster, name)
		
	def Server (self, uri, params = None, reqtype="rpc", headers = None, auth = None, encoding = None, use_cache = True, mapreduce = False, filter = None, callback = None):
		# reqtype: rpc, get, post, head, put, delete
		if type (headers) is list:
			h = {}
			for n, v in headers:
				h [n] = v
			headers = h
		
		if reqtype.endswith ("rpc"):
			return Proxy (ClusterDistCall, self.cluster, uri, params, reqtype, headers, auth, encoding, use_cache, mapreduce, filter, callback, self.cachesfs, self.logger)
		else:	
			return ClusterDistCall (self.cluster, uri, params, reqtype, headers, auth, encoding, use_cache, mapreduce, filter, callback, self.cachesfs, self.logger)
		
	
if __name__ == "__main__":
	from aquests.lib  import logger
	from . import cluster_manager
	import sys
	import asyncore
	import time
	from aquests.client import socketpool
	
	def _reduce (asyncall):
		for rs in asyncall.getswait (5):
			print("Result:", rs.id, rs.status, rs.code, repr(rs.result [:60]))
					
	def testCluster ():	
		sc = cluster_manager.ClusterManager ("tt", ["210.116.122.187:3424 1", "210.116.122.184:3424 1", "175.115.53.148:3424 1"], logger= logger.screen_logger ())
		clustercall = ClusterDistCallCreator (sc, logger.screen_logger ())	
		s = clustercall.Server ("rpc2", login = "admin/whddlgkr")
		s.bladese.util.status ("openfos.v2")		
		threading.Thread (target = _reduce, args = (s,)).start ()
		
		while 1:
			asyncore.loop (timeout = 1, count = 2)
			if len (asyncore.socket_map) == 1:
				break
	
	def testSocketPool ():
		sc = socketpool.SocketPool (logger.screen_logger ())
		clustercall = ClusterDistCallCreator (sc, logger.screen_logger ())			
		s = clustercall.Server ("http://www.bidmain.com/")
		s.request ()
		
		#s = clustercall.Server ("http://210.116.122.187:3424/rpc2", "admin/whddlgkr")
		#s.bladese.util.status ("openfos.v2")
		
		threading.Thread (target = __reduce, args = (s,)).start ()
		
		while 1:
			asyncore.loop (timeout = 1, count = 2)
			print(asyncore.socket_map)
			if len (asyncore.socket_map) == 1:
				break
	
	trigger.start_trigger ()
	
	testCluster ()
	testSocketPool ()
