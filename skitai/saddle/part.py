import re, sys
from functools import wraps
from urllib.parse import unquote_plus, quote_plus, urljoin
import os
from aquests.lib import importer, strutil
from types import FunctionType as function
import inspect
from skitai import was as the_was
import time, threading
from .storage import Storage
	
RX_RULE = re.compile ("(/<(.+?)>)")

class Part:
	def __init__ (self, *args, **kargs):			
		self.module = None
		self.packagename = None
		self.wasc = None				
		
		self.logger = None
		self.mount_p = "/"
		self.path_suffix_len = 0
		self.route_map = {}
		self.route_priority = []
		self.storage = Storage ()
		
		self.lock = threading.RLock ()
		self.init_time = time.time ()
		self._tempate_globals = {}
		
		self._binds_server = [None] * 5
		self._binds_request = [None] * 4
		self._binds_when = [None] * 5		
		self.handlers = {}
				
	def set_mount_point (self, mount):	
		if not mount:
			self.mount_p = "/"
		elif mount [-1] != "/":
			self.mount_p = mount + "/"
		else:
			self.mount_p = mount
		self.path_suffix_len = len (self.mount_p) - 1
				
	def init (self, module, packagename = "app", mount = "/"):
		self.module = module	
		self.packagename = packagename
		self.set_mount_point (mount)
		
		if self.module:
			self.abspath = self.module.__file__
			if self.abspath [-3:] != ".py":
				self.abspath = self.abspath [:-1]
			self.update_file_info	()
		
	def __getitem__ (self, k):
		return self.route_map [k]
	
	def log (self, msg):
		self.logger.log (msg)
	
	def trace (self):
		self.logger.trace ()
		
	def reload_package (self):
		importer.reloader (self.module)
		self.update_file_info	()
	
	def is_reloadable (self):
		if self.module is None: return False
		stat = os.stat (self.abspath)
		return self.file_info != (stat.st_mtime, stat.st_size)		
			
	def update_file_info (self):
		stat = os.stat (self.abspath)
		self.file_info = (stat.st_mtime, stat.st_size)
	
	#----------------------------------------------
	# App Decorators
	#----------------------------------------------
	def startup (self, f):
		self._binds_server [0] = f
		return f
	start_up = startup
	
	def reload (self, f):
		self._binds_server [1] = f
		return f
	onreload = reload
	
	def shutdown (self, f):
		self._binds_server [2] = f
		return f
	
	def mounted (self, f):
		self._binds_server [3] = f
		return f
	
	def umount (self, f):
		self._binds_server [4] = f
		return f
				
	def before_request (self, f):
		self._binds_request [0] = f
		return f
	
	def finish_request (self, f):
		self._binds_request [1] = f
		return f
	
	def failed_request (self, f):
		self._binds_request [2] = f
		return f
	
	def teardown_request (self, f):
		self._binds_request [3] = f
		return f
	
	def got_template (self, f):
		self._binds_when [0] = f
		return f
	
	def template_rendered (self, f):
		self._binds_when [1] = f
		return f
	
	def message_flashed (self, f):
		self._binds_when [2] = f
		return f
	
	#------------------------------------------------------
		
	def get_error_page (self, error):
		handler = self.handlers.get (error ['code'], self.handlers.get (0))
		if not handler:
			return
		was = the_was._get ()	
		# reset was.app for rendering
		was.app = self
		content =	handler [0] (was, error)
		was.app = None
		return content
		
	def add_error_handler (self, errcode, f, **k):
		self.handlers [errcode] = (f, k)		
		
	def errorhandler (self, errcode, **k):
		def decorator(f):
			self.add_error_handler (errcode, f, **k)
			@wraps(f)
			def wrapper (*args, **kwargs):
				return f (*args, **kwargs)
			return wrapper
		return decorator
	
	def defaulterrorhandler (self, f):
		self.add_error_handler (0, f)
		return f
			
	#----------------------------------------------
	# Event Binding
	#----------------------------------------------
	def when_got_template (self, *args):
		self._binds_when [0] and self._binds_when [0] (*args)
	
	def when_template_rendered (self, *args):
		self._binds_when [1] and self._binds_when [1] (*args)
		
	def when_message_flashed (self, *args):
		self._binds_when [2] and self._binds_when [2] (*args)
	
	#----------------------------------------------
	# URL Building
	#----------------------------------------------		
	def url_for (self, thing, *args, **kargs):
		if thing.startswith ("/"):
			return self.route [:-1] + self.mount_p [:-1] + thing
	
		for func, name, fuvars, favars, numvars, str_rule, options in self.route_map.values ():
			if thing != name: continue								
			assert len (args) <= len (fuvars), "Too many params, this has only %d params(s)" % len (fuvars)						
			params = {}
			for i in range (len (args)):
				assert fuvars [i] not in kargs, "Collision detected on keyword param '%s'" % fuvars [i]
				params [fuvars [i]] = args [i]				
			
			for k, v in kargs.items ():
				params [k] = v
			
			url = str_rule
			if favars: #fancy [(name, type),...]. /fancy/<int:cid>/<cname>
				for n, t in favars:
					if n not in params:
						raise AssertionError ("Argument '%s' missing" % n)
					value = quote_plus (str (params [n]))
					if t == "string":
						value = value.replace ("+", "_")
					elif t == "path":
						value = value.replace ("%2F", "/")
					url = url.replace ("<%s%s>" % (t != "string" and t + ":" or "", n), value)
					del params [n]
			
			if params:
				url = url + "?" + "&".join (["%s=%s" % (k, quote_plus (str(v))) for k, v in params.items ()])
				
			return self.url_for (url)
	
	def build_url (self, thing, *args, **kargs):
		url = self.url_for (thing, *args, **kargs)
		if url:
			return url			
	
	def template_global (self, name):
		def decorator (f):
			self._tempate_globals [name] = f
			@wraps(f)
			def wrapper (*args, **kwargs):
				return f (*args, **kwargs)
			return wrapper
		return decorator
	
	#----------------------------------------------
	# Routing
	#----------------------------------------------						
	def route (self, rule, **k):
		def decorator (f):
			self.add_route (rule, f, **k)
			@wraps(f)
			def wrapper (*args, **kwargs):
				return f (*args, **kwargs)
			return wrapper
		return decorator
			
	def get_route_map (self):
		return self.route_map
	
	def set_route_map (self, route_map):
		self.route_map = route_map
									
	def try_rule (self, path_info, rule, rulepack):
		f, n, l, a, c, s, options = rulepack
		
		arglist = rule.findall (path_info)
		if not arglist: 
			return None, None
		
		arglist = arglist [0]
		if type (arglist) is not tuple:
			arglist = (arglist,)
			
		kargs = {}
		for i in range(len(arglist)):
			an, at = a [i]
			if at == "int":
				kargs [an] = int (arglist [i])
			elif at == "float":
				kargs [an] = float (arglist [i])
			elif at == "path":
				kargs [an] = unquote_plus (arglist [i])
			else:		
				kargs [an] = unquote_plus (arglist [i]).replace ("_", " ")
		return f, kargs
	
	def add_route (self, rule, func, **options):					
		if not rule or rule [0] != "/":
			raise AssertionError ("Url rule should be starts with '/'")
		
		fspec = inspect.getargspec(func)
		options ["args"] = fspec.args [1:]
		options ["varargs"] = fspec.varargs
		options ["keywords"] = fspec.keywords
		
		if fspec.defaults:
			defaults = {}
			argnames = fspec.args[(len (fspec.args) - len (fspec.defaults)):]
			for i in range (len (fspec.defaults)):
				defaults [argnames [i]] = fspec.defaults[i]
			options ["defaults"] = defaults
		
		s = rule.find ("/<")
		if s == -1:	
			self.route_map [rule] = (func, func.__name__, func.__code__.co_varnames [1:func.__code__.co_argcount], None, func.__code__.co_argcount - 1, rule, options)						
		else:
			s_rule = rule
			rulenames = []
			urlargs = RX_RULE.findall (rule)
			options ["urlargs"] = len (urlargs)
			for r, n in urlargs:
				if n.startswith ("int:"):
					rulenames.append ((n[4:], n[:3]))
					rule = rule.replace (r, "/([0-9]+)")
				elif n.startswith ("float:"):
					rulenames.append ((n[6:], n [:5]))
					rule = rule.replace (r, "/([.0-9]+)")
				elif n.startswith ("path:"):
					rulenames.append ((n[5:], n [:4]))
					rule = rule.replace (r, "/(.+)")	
				else:
					rulenames.append ((n, "string"))
					rule = rule.replace (r, "/([^/]+)")
			rule = "^" + rule + "$"			
			re_rule = re.compile (rule)				
			self.route_map [re_rule] = (func, func.__name__, func.__code__.co_varnames [1:func.__code__.co_argcount], tuple (rulenames), func.__code__.co_argcount - 1, s_rule, options)
			self.route_priority.append ((s, re_rule))
			self.route_priority.sort (key = lambda x: x [0], reverse = True)			
	
	def get_routed (self, method_pack):
		if not method_pack: 
			return
		temp = method_pack
		while 1:
			routed = temp [1]
			if type (routed) is not list:
				return routed
			temp = routed
					
	def route_search (self, path_info):
		if not path_info:
			return self.url_for ("/"), self.route_map ["/"]
		if path_info in self.route_map:
			return self.route_map [path_info][0], self.route_map [path_info]
		trydir = path_info + "/"
		if trydir in self.route_map:
			return self.url_for (trydir), self.route_map [trydir]
		raise KeyError
					
	def get_package_method (self, path_info, command, content_type, authorization, use_reloader = False):		
		if not (path_info.startswith (self.mount_p) or (path_info + "/").startswith (self.mount_p)):
			return None, None, None, None, None, 0
		
		path_info = path_info [self.path_suffix_len:]
		app, method, kargs, matchtype = self, None, {}, 0				
		# 1st, try find in self
		try:			
			method, current_rule = self.route_search (path_info)
			
		except KeyError:
			for priority, rule in self.route_priority:
				current_rule = self.route_map [rule]
				method, kargs = self.try_rule (path_info, rule, current_rule)
				if method: 
					match = (rule, current_rule)
					matchtype = 2
					options = current_rule [-1]
					break
		
		else:			
			match = path_info
			if type (method) is not function:
				# object move
				matchtype = -1
			else:	
				matchtype = 1
				options = current_rule [-1]
		
		if method is None:
			return None, None, None, None, None, 0
		
		if matchtype == -1: # 301 move
			return app, method, None, None, None, -1
			
		return app, [self._binds_request [0], method] + self._binds_request [1:4], kargs, options, match, matchtype
	
	#----------------------------------------------
	# Django Model
	#----------------------------------------------
	
	def _model_changed (self, sender, **karg):
		model_name = str (sender)[8:-2]
		karg ['x_model_class'] = model_name
		if 'created' not in karg:
			karg ["x_operation"] = 'D'
		elif karg["created"]:
			karg ["x_operation"] = 'C'
		else:
			karg ["x_operation"] = 'U'
		karg ["x_ignore"]	= True
		the_was._get ().setlu (model_name, sender, **karg)
	
	def model_signal (self, modeler = "django"):	
		from django.db.models.signals import post_save, post_delete
		
		post_save.connect (self._model_changed)
		post_delete.connect (self._model_changed)
			
	#----------------------------------------------
	# Starting App
	#----------------------------------------------
	def cleanup (self):
		# initing app & packages		
		self._binds_server [2] and self._binds_server [2] (self.wasc)
	
	def onmounted (self, was)	:
		self._binds_server [3] and self._binds_server [3] (was)
	
	def onumount (self, was):
		self._binds_server [4] and self._binds_server [4] (was)
			
	def start (self, wasc, route):
		self.wasc = wasc
		
		if not route: 
			self.route = "/"
		elif not route.endswith ("/"):			
			self.route = route + "/"
		else:
			self.route = route			
		# initing app
		self._binds_server [0] and self._binds_server [0] (self.wasc)
	
	def restart (self, wasc, route):
		self.start (wasc, route)
			
