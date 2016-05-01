import threading 
import time
import os
import sys
from . import part, multipart_collector, cookie, secured_cookie_value
from . import wsgi_executor, xmlrpc_executor
from skitai.lib import producers
from skitai.server import utility
from hashlib import md5
import random
import base64
from skitai.saddle import cookie
JINJA2 = True
try:	
	from jinja2 import Environment, PackageLoader
except ImportError:
	JINJA2 = False
		
try:
	import xmlrpc.client as xmlrpclib
except ImportError:
	import xmlrpclib

multipart_collector.MultipartCollector.file_max_size = 20 * 1024 * 1024
multipart_collector.MultipartCollector.cache_max_size = 5 * 1024 * 1024
secured_cookie_value.Session.default_session_timeout = 1200

class Config:
	pass

class AuthorizedUser:
	def __init__ (self, user, realm):
		self.name = user
		self.realm = realm
		
				
class Saddle (part.Part):
	use_reloader = False
	debug = False
	
	# Session
	securekey = None
	session_timeout = None
	
	#WWW-Authenticate
	authorization = "digest"
	realm = None
	users = {}
	opaque = None
	
	def __init__ (self, module_name):
		part.Part.__init__ (self)
		self.module_name = module_name
		self.jinja_env = JINJA2 and Environment (loader = PackageLoader (module_name)) or None		
		self.lock = threading.RLock ()
		self.cache_sorted = 0
		self.cached_paths = {}
		self.cached_rules = []
		self.config = Config ()
	
	def jinja_overlay (self, line_statement = "%", variable_string = "#"):
		from . import jinjapatch
		
		if len (variable_string) == 1:
			jinjapatch.patch ()
		else:
			jinjapatch.unpatch ()
			
		self.jinja_env = self.jinja_env.overlay (
		  variable_start_string=variable_string,
		  variable_end_string=variable_string,
		  line_statement_prefix=line_statement,
		  line_comment_prefix=line_statement * 2
		)		
				
	def __setattr__ (self, name, attr):
		if name == "upload_file_max_size":
			multipart_collector.MultipartCollector.file_max_size = attr
		self.__dict__ [name] = attr
	
	def get_template (self, name):
		if JINJA2:
			return self.jinja_env.get_template (name)
		raise ImportError ("jinja2 required.")
			
	def get_www_authenticate (self):
		if self.authorization == "basic":
			return 'Basic realm="%s"' % self.realm
		else:	
			if self.opaque is None:
				self.opaque = md5 (self.realm.encode ("utf8")).hexdigest ()
			return 'Digest realm="%s", qop="auth", nonce="%s", opaque="%s"' % (
				self.realm, utility.md5uniqid (), self.opaque
			)
			
	def authorize (self, auth, method, uri):
		if self.realm is None or not self.users:
			return
				
		if auth is None:
			return self.get_www_authenticate ()
		
		# check validate: https://evertpot.com/223/
		amethod, authinfo = auth.split (" ", 1)
		if amethod.lower () != self.authorization:
			return self.get_www_authenticate ()
		
		if self.authorization == "basic":
			basic = base64.decodestring (authinfo.encode ("utf8")).decode ("utf8")
			current_user, password = basic.split (":", 1)
			if password == self.users.get (current_user):
				return AuthorizedUser (current_user, self.realm)
				
		else:
			method = method.upper ()
			infod = {}
			for info in authinfo.split (","):
				k, v = info.strip ().split ("=", 1)
				if not v: return self.get_www_authenticate ()
				if v[0] == '"': v = v [1:-1]
				infod [k]	 = v
			
			current_user = infod.get ("username")
			if not current_user:
				return self.get_www_authenticate ()
			
			password = self.users.get (current_user)
			if not password:
				return self.get_www_authenticate ()
				
			try:
				if uri != infod ["uri"]:					
					return self.get_www_authenticate ()
					
				A1 = md5 (("%s:%s:%s" % (infod ["username"], self.realm, password)).encode ("utf8")).hexdigest ()
				A2 = md5 (("%s:%s" % (method, infod ["uri"])).encode ("utf8")).hexdigest ()
				Hash = md5 (("%s:%s:%s:%s:%s:%s" % (
					A1, 
					infod ["nonce"],
					infod ["nc"],
					infod ["cnonce"],
					infod ["qop"],
					A2
					)).encode ("utf8")
				).hexdigest ()

				if Hash == infod ["response"]:
					return AuthorizedUser (current_user, self.realm)
					
			except KeyError:
				pass
		
		return self.get_www_authenticate ()
			
	def set_devel (self, debug = True, use_reloader = True):
		self.debug = debug
		self.use_reloader = use_reloader
	
	def get_multipart_collector (self):
		return multipart_collector.MultipartCollector
	
	def get_method (self, path_info):		
		current_app, method, kargs = self, None, {}
		self.lock.acquire ()
		try:
			method = self.cached_paths [path_info]
		except KeyError:
			for rulepack, freg in self.cached_rules:
				method, kargs = self.try_rule (path_info, rulepack)
				if method: 
					break
		finally:
			self.lock.release ()
	
		if not method:
			if self.use_reloader:
				self.lock.acquire ()																
			try:	
				current_app, method, kargs, match, matchtype = self.get_package_method (path_info, self.use_reloader)
			finally:	
				if self.use_reloader: 
					self.lock.release ()
			
			if not self.use_reloader:
				self.lock.acquire ()
				if matchtype == 1:
					self.cached_paths [match] = method
								
				elif matchtype == 2:
					self.cached_rules.append ([match, 1])
					if time.time () - self.cache_sorted > 300:
						self.cached_rules.sort (lambda x, y: cmp (y[1], x[1]))
						self.cached_rules.sort (key = lambda x: x[1], reverse = True)
						self.cache_sorted = time.time ()
				self.lock.release ()
					
			if matchtype == 3:				
				return current_app, method, 301
		
		return current_app, method, kargs
	
	def restart (self, wasc, route):
		self.start (wasc, route, self.packages)
	
	def create_on_demand (self, was, name):
		class G: pass
		# create just in time objects
		if name == "cookie":			
			return cookie.Cookie (was.request, self.securekey, self.session_timeout)
			
		if name in ("session", "mbox"):			
			if not was.in__dict__ ("cookie"):
				was.cookie = cookie.Cookie (was.request, self.securekey, self.session_timeout)			
			if name == "session":
				return was.cookie.get_session ()					
			if name == "mbox":
				return was.cookie.get_notices ()		
				
		if name == "g":
			return G ()
		
	def cleanup_on_demands (self, was):
		if was.in__dict__ ("g"):
			del was.g
		if not was.in__dict__ ("cookie"):
			return
		for j in ("session", "mbox"):
			if was.in__dict__ (j):		
				delattr (was, j)
		del was.cookie
										
	def __call__ (self, env, start_response):
		was = env ["skitai.was"]		
		was.app = self
		was.ab = self.build_url
		was.response = was.request.response
		
		content_type = env.get ("CONTENT_TYPE", "")				
		if content_type.startswith ("text/xml") or content_type.startswith ("application/xml+rpc"):
			result = xmlrpc_executor.Executor (env, self.get_method) ()
		else:	
			result = wsgi_executor.Executor (env, self.get_method) ()		
		
		del was.response
		del was.ab		
		self.cleanup_on_demands (was) # del session, mbox, cookie, g
			
		return result
		