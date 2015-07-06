import sys, os
import urllib
import sys
from skitai.server import utility, http_cookie
from skitai.server.threads import trigger
from skitai.lib import udict

header2env = {
	'content-length'	: 'CONTENT_LENGTH',
	'content-type'	  : 'CONTENT_TYPE',
	'connection'		: 'CONNECTION_TYPE'
	}


MAX_POST_SIZE = 5242880
MAX_UPLOAD_SIZE = 2147483648L

def catch (htmlformating = 0):
	t, v, tb = sys.exc_info()
	tbinfo = []
	assert tb # Must have a traceback
	while tb:
		tbinfo.append((
			tb.tb_frame.f_code.co_filename,
			tb.tb_frame.f_code.co_name,
			str(tb.tb_lineno)
			))
		tb = tb.tb_next

	del tb
	file, function, line = tbinfo [-1]
	
	if htmlformating:
		buf = ["<hr><h3 style='color:#d90000;'>%s <span style='color:#666666;'>%s</span></h3>" % (str (t).replace (">", "&gt;").replace ("<", "&lt;"), v)]
		buf.append ("<b>In %s at line %s, %s</b>" % (file, line, function == "?" and "__main__" or "function " + function))
		buf.append ("<ul type='square'>")
		buf += ["<li><i>%s</i> &nbsp;&nbsp;<font color='#d90000'>%s</font> <font color='#003366'><b>%s</b></font></li>" % x for x in tbinfo]
		buf.append ("</ul>")		
		return "\n".join (buf)
		
	else:
		buf = []
		buf.append ("%s %s" % (t, v))
		buf.append ("In %s at line %s, %s" % (file, line, function == "?" and "__main__" or "function " + function))
		buf += ["%s %s %s" % x for x in tbinfo]
		return "\n".join (buf)
		

class Collector:
	def __init__ (self, handler, request):
		self.handler = handler
		self.request = request
		self.data = ''
		self.content_length = self.get_content_length ()
	
	def get_content_length (self):
		cl = self.request.get_header ('content-length')
		if cl is not None:
			try:
				cl = int  (cl)
			except (ValueError, TypeError):
				cl = None
		return cl
		
	def start_collect (self):	
			if self.content_length == 0: 
				return self.found_terminator()
			self.request.channel.set_terminator (self.content_length)

	def collect_incoming_data (self, data):
		self.data = self.data + data

	def found_terminator (self):
		# prepare got recving next request header
		self.request.collector = None  # break circ. ref
		self.request.set_body (self.data)
		self.request.channel.set_terminator ('\r\n\r\n')
		self.handler.continue_request (self.request, self.data)
	
	def abort (self):
		self.data = ""
		self.request.collector = None  # break circ. ref
	

class Handler:
	GATEWAY_INTERFACE = 'SSGI/0.9'
	
	def __init__(self, wasc):
		self.wasc = wasc
		
	def match (self, request):
		return 1
			
	def build_environ (self, request):
		env = {}
		(path, params, query, fragment) = request.split_uri()
		if params: path = path + params

		while path and path[0] == '/':
			path = path[1:]
		
		if '%' in path: path = urllib.unquote (path)		
		if query: query = query[1:]

		server_inst = self.wasc.httpserver
		env = udict.UDict ()
		env ['REQUEST_METHOD'] = request.command.upper()
		env ['SERVER_PORT'] = str (server_inst.port)
		env ['SERVER_NAME'] = server_inst.server_name
		env ['SERVER_SOFTWARE'] = server_inst.SERVER_IDENT
		env ['SERVER_PROTOCOL'] = "HTTP/" + request.version
		env ['CHANNEL_CREATED'] = request.channel.creation_time
		env ['SCRIPT_NAME'] = '/' + path
		if query: env['QUERY_STRING'] = query		
		env ['REMOTE_ADDR'] = request.channel.addr [0]
		env ['REMOTE_SERVER'] = request.channel.addr [0]
		
		env_has = env.has_key
		for header in request.header:
			key, value = header.split(":", 1)
			key = key.lower()
			value = value.strip ()
			if header2env.has_key (key) and value:
				env [header2env [key]] = value				
			else:
				key = 'HTTP_%s' % ("_".join (key.split ( "-"))).upper()
				if value and not env.has_key (key):
					env [key] = value
		
		for k, v in os.environ.items ():
			if not env.has_key (k):
				env [k] = v
				
		return env
	
	def has_permission (self, request, app):
		permission = app.get_permission ()
		if permission and not self.wasc.authorizer.has_permission (request, permission): # request athorization
			return False
		return True
	
	def parse_args (self, query, data):
		args = {}
		if query: args = utility.crack_query (query)
		if data:
			for k, v in utility.crack_query (data).items ():
				if args.has_key (k):
					if type (args [k]) is not type ([]):
						args [v] = [args [k]]
					args [v].append (v)
				else:	 
					args [k] = v
		return args
	
	def create_was (self, request, app):
		was = self.wasc ()
		was.request = request
		was.response = was.request.response		
		was.cookie = http_cookie.Cookie (request)
		was.app = app
		was.env = self.build_environ (request)
		was.env ['GATEWAY_INTERFACE'] = self.GATEWAY_INTERFACE		
		if app.is_session_enabled ():
			was.session = was.cookie.get_session ()
		else:
			was.session = None
		return was
	
	def make_collector (self, collector_class, request, max_cl = MAX_POST_SIZE, *args, **kargs):
		collector = collector_class (self, request, *args, **kargs)
		if collector.content_length is None:
			request.response.error (411)
			return
			
		elif collector.content_length > max_cl: #5M
			self.wasc.logger ("server", "too large request body (%d)" % collector.content_length, "wran")
			if request.get_header ("expect") == "100-continue":							
				request.response.error (413) # client doesn't send data any more, I wish.
			else:
				request.response.abort (413)	# forcely disconnect
			return
		
		# ok. allow form-data
		if request.get_header ("expect") == "100-continue":
			request.response.instant (100)

		return collector

	def handle_request (self, request):
		path, params, query, fragment = request.split_uri ()
		if self.wasc.apps.has_route (path) == -1:
			request.response ["Location"] = "%s/%s%s" % (
				path, 
				params and params or "",
				query and query or ""
			)
			if request.command in ('post', 'put'):
				request.response.abort (301)
			else:	
				request.response.error (301)
				return
		
		if request.command in ('post', 'put'):
			collector = self.make_collector (Collector, request, MAX_POST_SIZE)
			if collector:
				request.collector = collector
				collector.start_collect ()
									
		elif request.command in ('get', 'delete'):
			self.continue_request(request)
			
		else:
			request.response.error (405)
	
	def continue_request (self, request, data = None):
		try:
			path, params, query, fragment = request.split_uri ()
			method, app = self.wasc.apps.get_app (path)
					
		except:
			self.wasc.logger.trace ("server",  request.uri)
			return request.response.error (500, catch (1))
		
		if not method:
			return request.response.error (404)
		
		try:
			ct = request.get_header ("content-type")
			if ct is None: ct = ""
				
			if request.command == "get" or ct.startswith ("application/x-www-form-urlencoded"):
				args = self.parse_args (query, data)
				was = self.create_was (request, app)
				
			elif ct.startswith ("multipart/form-data"):
				args = data
				# cached form data string if size < 10 MB
				# it used for relay small files to the others				
				for k, v in self.parse_args (query, None).items ():
					args [k] = v				
				was = self.create_was (request, app)
				
			else:	# xml, json
				args = {}
				was = self.create_was (request, app)
							
		except:
			self.wasc.logger.trace ("server",  request.uri)
			return request.response.error (500, catch (1))
			
		self.wasc.queue.put (Job (was, path, method, args))
				
		
_STRING_TYPES = (type (""), type (u""))
	
class Job:
	def __init__(self, was, muri, method, args):
		self.was = was
		self.muri = muri
		self.method = method
		self.args = args
		
	def __repr__(self):
		return "<Job %s %s HTTP/%s>" % (self.was.request.command.upper (), self.was.request.uri, self.was.request.version)
	
	def __str__ (self):
		return "%s %s HTTP/%s" % (self.was.request.command.upper (), self.was.request.uri, self.was.request.version)
		
	def dealloc (self):
		self.was.cookie = None
		self.was.session = None
		self.was.request.response = None
		self.was.request = None
		self.was.environ = None
		self.was.app = None		
		del self.was
	
	def get_response (self, method, args):
		# recursive before, after, teardown
		# [b, [b, [b, func, a, t], a, t], a t]
		
		response = None
			
		[before, func, after, teardown], uargs = method
		if before:
			response = before (self.was)
			if response:
				return response
		
		try:
			if type (func) is type ([]):
				response = self.get_response ((func, uargs), args)					
					
			else:
				if type (args)==type({}):
					if uargs: # url args
						for k, v in uargs.items ():
							args [k] = v			
					response = func (self.was, **args)
				else:
					response = func (self.was, *args, **uargs)
		
		except MemoryError:
			raise
																										
		except:
			self.was.logger.trace ("app", str (self))
			if teardown:
				response = teardown (self.was)
							
		else:
			if after: 
				try: after (self.was)
				except:	self.was.logger.trace ("app")
			
		return response	
	
	def commit_all (self):
		# keep commit order, session -> cookie
		try: self.was.session.commit ()
		except AttributeError: pass							
		self.was.cookie.commit ()
		
	def __call__(self):
		try:			
			response = self.get_response (self.method, self.args)
			
		except MemoryError:
			raise
				
		except:	
			self.was.logger.trace ("app", str (self))
			trigger.wakeup (lambda p=self.was.response, d=catch(1): (p.error (500, d),))
		
		else:
			if not self.was.response.is_sent_response:
				try:
					self.commit_all ()
								
				except:
					self.was.logger.trace ("server")
					trigger.wakeup (lambda p=self.was.response, d=catch(1): (p.error (500, d),))
	
				else:
					if not self.was.request.response.has_key ("content-type"):
						self.was.request.response.update ('Content-Type', "text/html")				
					
					if type (response) in _STRING_TYPES:			
						self.was.request.response.update ('Content-Length', len (response))			
						trigger.wakeup (lambda p=self.was.response, d=response: (p.push(d), p.done()))
			
					elif hasattr (response, "more"): # producer (ex: producers.stream_producer)
						if hasattr (response, "abort"):
							self.was.request.producer = response
						trigger.wakeup (lambda p=self.was.response, d=response: (p.push(d), p.done()))
								
					else:
						try:
							raise ValueError, "Content should be string or producer type"
						except:
							self.was.logger.trace ("app")
							trigger.wakeup (lambda p=self.was.response, d=catch(1): (p.error (500, d),))
							
		self.dealloc ()
