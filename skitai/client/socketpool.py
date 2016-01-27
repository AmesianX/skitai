﻿import threading
from skitai.server.threads import socket_map
from . import asynconnect
import time
try:
	from urllib.parse import urlparse
except ImportError:
	from urlparse import urlparse	
import copy

class SocketPool:
	maintern_interval = 60
	object_timeout = 120
	
	def __init__ (self, logger):
		self.__socketfarm = {}
		self.__numget  = 0
		self.__last_maintern = time.time ()
		self.logger = logger
		self.lock = threading.RLock ()
		self.numobj = 0
	
	def match (self, request):
		return False		
	
	def get_name (self):
		return "__socketpool__"
				
	def status (self):
		info = {}
		cluster = {}
		self.lock.acquire ()
		info ["numget"] = self.__numget
				
		try:
			try:	
				for serverkey, node in list(self.__socketfarm.items ()):	
					nnode = {}
					nnode ["numactives"] = len ([x for x in list(node.values ()) if x.isactive ()])
					nnode ["numconnecteds"] = len ([x for x in list(node.values ()) if x.isconnected ()])
					conns = []
					for asyncon in list(node.values ()):
						stu = {
							"class": asyncon.__class__.__name__, 
							"connected": asyncon.isconnected (), 
							"isactive": asyncon.isactive (), 
							"request_count": asyncon.get_request_count (),
							"event_time": time.asctime (time.localtime (asyncon.event_time)), 
							"zombie_timeout": asyncon.zombie_timeout,								
						}
						try: stu ["has_result"] = asyncon.has_result
						except AttributeError: pass						
						
						try: 
							di = asyncon.debug_info
							if di:
								stu ["debug_info"] = "%s %s HTTP/%s" % asyncon.debug_info
						except AttributeError: pass
						try:
							stu ["in_map"] = asyncon.is_channel_in_map ()
						except AttributeError: pass	
							
						conns.append (stu)
													
					nnode ["connections"] = conns
					cluster [serverkey] = nnode
					
			finally:
				self.lock.release ()
				
		except:
			self.logger.trace ()
					
		info ["cluster"] = cluster
		return info
		
	def report (self, asyncon, well_functioning):
		pass # for competitable
	
	def get_nodes (self):
		if not self.__socketfarm: return [None] # at least one item needs
		return list(self.__socketfarm.items ())
		
	def maintern (self):
		try:			
			# close unused sockets
			for serverkey, node in list(self.__socketfarm.items ()):
				for _id, asyncon in list(node.items ()):					
					if hasattr (asyncon, "maintern"):
						asyncon.maintern ()
						
					try:
						closed = asyncon.is_deletable (self.object_timeout) # keep 2 minutes		
					except:
						self.logger.trace ()
						closed = False
						
					if closed:
						del self.__socketfarm [serverkey][_id]
						del asyncon
						self.numobj -= 1
						
				if not self.__socketfarm [serverkey]:
					del self.__socketfarm [serverkey]
					
		except:
			self.logger.trace ()
		
		self.__last_maintern = time.time ()
	
	def _get (self, serverkey, server, *args):
		asyncon = None
			
		self.lock.acquire ()
		try:
			try:
				if time.time () - self.__last_maintern > self.maintern_interval:
					self.maintern ()
							
				self.__numget += 1
				if serverkey not in self.__socketfarm:
					asyncon = self.create_asyncon (server, *args)
					self.__socketfarm [serverkey] = {}
					self.__socketfarm [serverkey][id (asyncon)] = asyncon
					
				else:		
					for each in list(self.__socketfarm [serverkey].values ()):	
						if not each.isactive ():
							asyncon = each
							break
					
					if not asyncon:
						asyncon = self.create_asyncon (server, *args)
						self.__socketfarm [serverkey][id (asyncon)] = asyncon
				
				asyncon.set_active (True, nolock = True)
			
			finally:
				self.lock.release ()
		
		except:
			self.logger.trace ()
		
		return asyncon
	
	def create_asyncon (self, server, scheme):
		if scheme in ("https", "wss"):
			__conn_class = asynconnect.AsynSSLConnect
			__dft_Port = 443
		elif scheme == "tunnel":
			__conn_class = asynconnect.AsynConnect
			__dft_Port = 443
		elif scheme == "proxy":
			__conn_class = asynconnect.AsynConnect			
			__dft_Port = 5000
		elif scheme == "proxys":
			__conn_class = asynconnect.AsynSSLProxyConnect
			__dft_Port = 5000				
		else:
			__conn_class = asynconnect.AsynConnect
			__dft_Port = 80
		
		try:
			addr, port = server.split (":", 1)
			port = int (port)
		except ValueError:
			addr, port = server, __dft_Port
		
		self.numobj += 1			
		asyncon = __conn_class ((addr, port), self.lock, self.logger)	
		if scheme == "proxy":
			asyncon.set_proxy (True)
		return asyncon
				
	def get (self, uri):	
		scheme, server, script, params, qs, fragment = urlparse (uri)
		serverkey = "%s://%s" % (scheme, server)		
		return self._get (serverkey, server, scheme)
		
	def cleanup (self):
		self.lock.acquire ()
		try:
			try:
				for server in list(self.__socketfarm.keys ()):					
					for asyncon in list(self.__socketfarm [server].values ()):
						asyncon.disconnect ()
			finally:
				self.lock.release ()
		except:
			self.logger.trace ()	


socketpool = None

def create (logger):
	global socketpool
	if socketpool is None:
		socketpool = SocketPool (logger)

def cleanup ():
	global socketpool
	socketpool.cleanup ()
		