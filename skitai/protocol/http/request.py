try:
	import xmlrpc.client as xmlrpclib
except ImportError:
	import xmlrpclib
	
import base64
try: 
	from urllib.parse import urlparse, quote
except ImportError:
	from urllib import quote
	from urlparse import urlparse
				
from skitai.lib import producers, strutil
import skitai

class XMLRPCRequest:	
	user_agent = "Mozilla/5.0 (compatible; Skitai/%s.%s)" % skitai.version_info [:2]
			
	def __init__ (self, uri, method, params = (), headers = None, encoding = "utf8", auth = None, logger = None):
		self.uri = uri
		self.method = method
		self.params = params		
		self.encoding = encoding
		self.auth = (auth and type (auth) is not tuple and tuple (auth.split (":", 1)) or auth)
		self.logger = logger
		self.address, self.path = self.split (uri)
		self.__xmlrpc_serialized = False
		self.headers = {
			"Accept": "*/*",
			"Accept-Encoding": "gzip"				
		}
		if headers:
			for k, v in headers.items ():
				n = k.lower ()
				if n in ("accept-encoding	", "content-length", "connection"):
					# reanalyze
					continue					
				self.headers [k] = v			
		self.data = self.serialize ()
	
	def build_header (self):
		if self.get_header ("host") is None:
			address = self.get_address ()
			if address [1] in (80, 443):			
				self.headers ["Host"] = "%s" % address [0]
			else:
				self.headers ["Host"] = "%s:%d" % address
		
		if self.get_header ("user-agent") is None:
			self.headers ["User-Agent"] = self.user_agent
		
	def get_cache_key (self):
		if len (self.data) > 4096:
			return None			
		return "%s:%s%s/%s" % (
			self.address [0], self.address [1],
			self.path, self.method
		)
		
	def xmlrpc_serialized (self):
		return self.__xmlrpc_serialized
		
	def set_address (self, address):
		self.address = address
		
	def get_address (self):
		return self.address
		
	def get_method (self):
		return "POST"
		
	def split (self, uri):
		if uri.find ("://") == -1:
			return None, uri
			
		scheme, address, script, params, qs, fragment = urlparse (uri)		
		if not script: script = "/"
		path = script
		if params: path += ";" + params
		if qs: path += "?" + qs
		
		try: 
			host, port = address.split (":", 1)
			port = int (port)
		except ValueError:
			host = address
			if scheme in ("http", "ws"):
				port = 80
			else:
				port = 443	
		
		return (host, port)	, path
		
	def serialize (self):
		self.__xmlrpc_serialized = True
		data = xmlrpclib.dumps (self.params, self.method, encoding=self.encoding, allow_none=1).encode ("utf8")
		self.headers ["Content-Type"] = "text/xml"
		self.headers ["Content-Length"] = len (data)
		return data
	
	def get_auth (self):
		return self.auth
		
	def get_data (self):
		return self.data
	
	def get_header (self, k, with_key = False):
		if self.headers:
			k = k.lower ()
			for n, v in self.headers.items ():
				if n.lower () == k:
					if with_key:
						return n, v
					return v
					
		if with_key:
			return None, None		
		
	def get_headers (self):
		self.build_header ()
		return list (self.headers.items ())
			
	
class HTTPRequest (XMLRPCRequest):
	def get_method (self):
		return self.method.upper ()
	
	def get_cache_key (self):
		if len (self.data) > 4096:
			return None			
		return "%s:%s%s" % (
			self.address [0], self.address [1], self.path
		)
	
	def to_bytes (self, set_content_length = True):
		if strutil.is_encodable (self.params):
			data = self.params.encoding ("utf8")		
		elif self.encoding and strutil.is_decodable (self.params):
			data = self.params.decode (self.encoding).encoding ("utf8") 		
		else:	
			data = self.params
		
		if set_content_length:
			# when only bytes type, in case proxy_request this value will be just bool type
			try:
				self.headers ["Content-Length"] = len (data)
			except TypeError:
				pass
		return data
	
	def urlencode (self, to_bytes = True):
		fm = []
		for k, v in list(self.params.items ()):
			if self.encoding:
				k = k.decode (self.encoding)
				v = v.decode (self.encoding)
			fm.append ("%s=%s" % (quote (k), quote (v)))				
		if to_bytes:	
			return "&".join (fm).encode ("utf8")
		return "&".join (fm)
	
	def nvpencode (self):
		fm = []
		for k, v in list(self.params.items ()):
			if self.encoding:
				k = k.decode (self.encoding)
				v = v.decode (self.encoding)
			v = v.encode ("utf8")
			fm.append (k.encode ("utf8") + b"[" + str (len (v)).encode ("utf8") + b"]=" + v)
		return b"&".join (fm)
									
	def serialize (self):
		# formdata type can be string, dict, boolean
		if not self.params:
			if self.get_method () in ("POST", "PUT"):
				self.headers ["Content-Length"] = 0
			return b""
		
		if self.get_method () in ("GET", "DELETE"):
			if type (self.params) is dict:
				params = self.urlencode (to_bytes = False)
			else:
				params = self.params
			self.uri += "?" + params
			self.path += "?" + params
			self.params = None
			return b""
			
		header_name, content_type = self.get_header ("content-type", True)
		if content_type is None:
			content_type = "application/json"
			self.headers ["Content-Type"] = content_type
				
		if type (self.params) is dict:			
			if content_type == "application/json":
				data = json.dumps (self.params).encode ("utf8")
				self.headers [header_name] = "application/json; charset=utf-8"
			elif content_type == "application/x-www-form-urlencoded":
				data = self.urlencode ()
				self.headers [header_name] = "application/x-www-form-urlencoded; charset=utf-8"
			elif content_type == "text/namevalue":
				data = self.nvpencode ()	
				self.headers [header_name] = "text/namevalue; charset=utf-8"
			else:	
				raise TypeError ("Unknown Content-Type")
			self.headers ["Content-Length"] = len (data)
			return data
		
		data = self.to_bytes ()
		return data
		
		
class HTTPMultipartRequest (HTTPRequest):
	boundary = "-------------------Skitai-%s.%s-a1a80da4-ca3d-11e6-b245-001b216d6e71" % skitai.version_info [:2]
		
	def __init__ (self, uri, method, params = {}, headers = None, encoding = None, auth = None, logger = None):
		HTTPRequest.__init__ (self, uri, method, params, headers, encoding, auth, logger)
		if type (self.params) is bytes:
			self.find_boundary ()
	
	def get_cache_key (self):
		return None
		
	def get_method (self):
		return "POST"
			
	def find_boundary (self):
		s = self.params.find (b"\r\n")
		if s == -1:
			raise ValueError("Boundary Not Found")
		b = self.params [:s]			
		if b [:2] != b"--":
			raise ValueError("invalid multipart/form-data")
		self.boundary = b [2:s]
		
	def serialize (self):
		self.headers ["Content-Type"] = "multipart/form-data; boundary=" + self.boundary
		if type (self.params) is dict:
			p = producers.multipart_producer (self.params, self.boundary, self.encoding)
			self.headers ["Content-Length"] = p.get_content_length ()
			return p
		data = self.to_bytes ()		
		return data
	
