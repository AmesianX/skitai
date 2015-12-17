import sys, os
try:
	from urllib.parse import unquote
except ImportError:
	from urllib import unquote	
import sys
from skitai.server import utility
from skitai.lib import producers
from skitai.server.http_response import catch
from skitai.server.threads import trigger
from . import collectors
from skitai import version_info, was as the_was
from skitai.saddle import Saddle
try:
	from cStringIO import StringIO as BytesIO
except ImportError:
	from io import BytesIO

header2env = {
	'content-length'	: 'CONTENT_LENGTH',
	'content-type'	  : 'CONTENT_TYPE',
	'connection'		: 'CONNECTION_TYPE'
	}

PY_MAJOR_VERSION = sys.version_info.major

class Handler:
	GATEWAY_INTERFACE = 'CGI/1.1'
	ENV = {
			'GATEWAY_INTERFACE': 'CGI/1.1',
			'SERVER_SOFTWARE': "Skitai App Engine/%s.%s.%s Python/%d.%d" % (version_info [:3] + sys.version_info[:2]),
			'skitai.version': tuple (version_info [:3]),			
			"wsgi.version": (1, 0),
			"wsgi.errors": sys.stderr,
			"wsgi.run_once": False,
			"wsgi.input": None
	}
	
	post_max_size = 5 * 1024 * 1024	
	upload_max_size = 100 * 1024 * 1024
	
	def __init__(self, wasc, post_max_size = 0, upload_max_size = 0):
		self.wasc = wasc
		if post_max_size: self.post_max_size = post_max_size
		if upload_max_size: self.upload_max_size = upload_max_size
		
		self.ENV ["skitai.threads"] = (self.wasc.config.getint ("server", "processes"), self.wasc.config.getint ("server", "threads")),		
		self.ENV ["wsgi.url_scheme"] = hasattr (self.wasc.httpserver, "ctx") == "https" or "http"
		self.ENV ["wsgi.multithread"] = hasattr (self.wasc, "threads")
		self.ENV ["wsgi.multiprocess"] = self.wasc.config.getint ("server", "processes") > 1 and os.name != "nt"
		self.ENV ['SERVER_PORT'] = str (self.wasc.httpserver.port)
		self.ENV ['SERVER_NAME'] = self.wasc.httpserver.server_name
		
	def match (self, request):
		return 1
			
	def build_environ (self, request, apph):	
		(path, params, query, fragment) = request.split_uri()
		if params: path = path + params
		while path and path[0] == '/':
			path = path[1:]		
		if '%' in path: path = unquote (path)			
		if query: query = query[1:]
		
		env = self.ENV.copy ()
		env ['REQUEST_METHOD'] = request.command.upper()		
		env ['SERVER_PROTOCOL'] = "HTTP/" + request.version
		env ['CHANNEL_CREATED'] = request.channel.creation_time
		if query: env['QUERY_STRING'] = query		
		env ['REMOTE_ADDR'] = request.channel.addr [0]
		env ['REMOTE_SERVER'] = request.channel.addr [0]		
		env ['SCRIPT_NAME'] = apph.route		
		env ['PATH_INFO'] = apph.get_path_info ("/" + path)
				
		for header in request.header:
			key, value = header.split(":", 1)
			key = key.lower()
			value = value.strip ()
			if key in header2env and value:
				env [header2env [key]] = value				
			else:
				key = 'HTTP_%s' % ("_".join (key.split ( "-"))).upper()
				if value and key not in env:
					env [key] = value
		
		for k, v in list(os.environ.items ()):
			if k not in env:
				env [k] = v
		
		return env
	
	def make_collector (self, collector_class, request, max_cl, *args, **kargs):
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
			request.response.instant ("100 Continue")

		return collector

	def handle_request (self, request):
		path, params, query, fragment = request.split_uri ()
		
		has_route = self.wasc.apps.has_route (path)
		if has_route == 0:
			return request.response.error (404)
		elif has_route in (1, 2):
			if has_route == 1:
				location = "%s/" % path
			else:
				location = path	[:-1]
			request.response ["Location"] = location
			
			if request.command in ('post', 'put'):
				return request.response.abort (301)
			else:	
				return request.response.error (301)
		
		app = self.wasc.apps.get_app (has_route).get_callable()
		try: 
			www_authenticate = app.authorize (request.get_header ("Authorization"), request.command, request.uri)
			if www_authenticate:
				request.response ['WWW-Authenticate'] = www_authenticate
				return request.response.error (401)
		except AttributeError: 
			pass
		
		ct = request.get_header ("content-type")
		if request.command == 'post' and ct and ct.startswith ("multipart/form-data"):
			# handle stream by app
			# shoud have constructor __init__ (self, handler, request, upload_max_size = 100000000)
			try:
				#self.wasc.apps.get_app (has_route) - module (that has callable) wrapper
				#.get_callable() - callable, like WSGI function, Saddle or Falsk app
				AppCollector = app.get_multipart_collector ()
			except AttributeError:
				AppCollector = None
					
			if AppCollector:
				collector = self.make_collector (AppCollector, request, self.upload_max_size, self.upload_max_size)
			else:
				collector = self.make_collector (collectors.MultipartCollector, request, self.upload_max_size, self.upload_max_size)	

			if collector:
				request.collector = collector
				collector.start_collect ()
			
		elif request.command in ('post', 'put'):
			collector = self.make_collector (collectors.FormCollector, request, self.post_max_size)
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
			apph = self.wasc.apps.get_app (path)
					
		except:
			self.wasc.logger.trace ("server",  request.uri)
			return request.response.error (500, why = apph.debug and catch (1) or "")
		
		try:
			env = self.build_environ (request, apph)
			if data:
				env ["wsgi.input"] = data
			args = (env, request.response.start_response)
								
		except:
			self.wasc.logger.trace ("server",  request.uri)
			return request.response.error (500, why = apph.debug and catch (1) or "")
		
		if env ["wsgi.multithread"]:
			self.wasc.queue.put (Job (request, apph, args, self.wasc.logger))
		else:
			Job (request, apph, args, self.wasc.logger) ()



class Job:
	# Multi-Threaded Jobs
	def __init__(self, request, apph, args, logger):
		self.request = request
		self.apph = apph
		self.args = args
		self.logger = logger
		
	def __repr__(self):
		return "<Job %s %s HTTP/%s>" % (self.request.command.upper (), self.request.uri, self.request.version)
	
	def __str__ (self):
		return "%s %s HTTP/%s" % (self.request.command.upper (), self.request.uri, self.request.version)
	
	def exec_app (self):		
		was = the_was._get ()
		self.args [0]["skitai.was"] = was
		
		request = was.request = self.request
		response = request.response
		
		try:
			content = self.apph (*self.args)
			
			if not response.responsable ():
				# already called response.done () or dicinnected channel
				return
				
			if content is None: # Possibly no return mistake
				raise AssertionError ("Content or part should not be None")

			if response ["content-type"] is None: 
				response ["Content-Type"] = "text/html"
			
			if not content: # explicit empty not content
				trigger.wakeup (lambda p=response: (p.done(),))
				return
			
			if type (content) not in (list, tuple):
				content = (content,) # make iterable
				
			will_be_push = []
			if len (response) == 0:
				content_length = 0
			else:
				content_length = None
					
			for part in content:
				if hasattr (part, "read"):
					part = producers.closing_stream_producer (part)	
				elif hasattr (part, "_next") or hasattr (part, "next"): # flask etc.
					part = producers.closing_iter_producer (part)
					
				if isinstance (part, producers.simple_producer) or hasattr (part, "more"):
					content_length = None
					# streaming obj
					if hasattr (part, "close"):
						# automatic close	when channel closing
						response.add_closable (part)
					will_be_push.append (part)
				
				else:
					type_of_part = type (part)
					#if (PY_MAJOR_VERSION >=3 and type_of_part is str) or (PY_MAJOR_VERSION <3 and type_of_part is unicode):
					if type_of_part is not bytes: # unicode
						part = part.encode ("utf8")
						type_of_part = bytes
						
					if type_of_part is bytes:
						if content_length is not None:
							content_length += len (part)
						will_be_push.append (part)
						
					else:
						raise AssertionError ("Streaming content should be single element")
			
			if content_length is not None:
				response ["Content-Length"]	= content_length
				
		except MemoryError:
			raise
			
		except:
			self.logger.trace ("app")
			trigger.wakeup (lambda p=response, d=self.apph.debug and catch (1) or "": (p.send_error ("500 Internal Server Error", d), p.done ()) )			
				
		else:
			for part in will_be_push:				
				if len (will_be_push) == 1 and type (part) is bytes and len (response) == 0:
					response.update ("Content-Length", len (part))
				response.push (part)					
			trigger.wakeup (lambda p=response: (p.done(),))
											
	def __call__(self):
		try:
			try:
				self.exec_app ()
			finally:
				self.deallocate	()
		except:
			# no response, alredy done. just log
			self.logger.trace ("server",  self.request.uri)
	
	def deallocate (self):
		env = self.args [0]		
		_input = env ["wsgi.input"]
		if _input:
			try: _input.close ()
			except AttributeError: pass
			if hasattr (_input, "name"):
				try: os.remove (_input.name)
				except: self.logger.trace ("app")
		
		was = env.get ("skitai.was")
		if was is not None:
			if hasattr (was, "app"): # Saddle
				was.cookie = None
				was.session = None
				was.response = None
				was.env = None
				was.ab = None
				was.app = None				
			
			if was.request:
				was.request.response = None
				was.request = None
