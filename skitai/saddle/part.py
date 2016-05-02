import re, sys
try:
	from urllib.parse import unquote_plus, quote_plus, urljoin
except ImportError:
	from urllib import unquote_plus, quote_plus
	from urlparse import urljoin
import os
from skitai.lib import importer, strutil
from types import FunctionType as function

	
RX_RULE = re.compile ("(/<(.+?)>)")

class Part:
	def __init__ (self, *args, **kargs):			
		self.module = None
		self.packagename = None
		self.wasc = None		
		self.packages = {}
		
		self.logger = None
		self.mount_p = "/"
		self.route_map = {}
		self._binds_server = [None] * 3
		self._binds_request = [None] * 4
		self._binds_when = [None] * 5		
	
	def render (self, was, template_file, _do_not_use_this_variable_name_ = {}, **karg):
		while template_file and template_file [0] == "/":
			template_file = template_file [1:]	
											
		if _do_not_use_this_variable_name_: 
			assert not karg, "Can't Use Dictionary and Keyword Args Both"
			karg = _do_not_use_this_variable_name_

		karg ["was"] = was		
		template = self.get_template (template_file)
		self.when_got_template (was, template, karg)
			
		rendered = template.render (karg)
		self.when_template_rendered (was, template, karg, rendered)
		return rendered	
				
	def set_mount_point (self, mount):	
		if not mount:
			self.mount_p = "/"
		elif mount [-1] != "/":
			self.mount_p = mount + "/"
		else:
			self.mount_p = mount
				
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
	# Decorators
	#----------------------------------------------
	def startup (self, f):
		self._binds_server [0] = f
	
	def onreload (self, f):
		self._binds_server [1] = f
		
	def shutdown (self, f):
		self._binds_server [2] = f
		
	def before_request (self, f):
		self._binds_request [0] = f
	
	def finish_request (self, f):
		self._binds_request [1] = f
	
	def failed_request (self, f):
		self._binds_request [2] = f
	
	def teardown_request (self, f):
		self._binds_request [3] = f
	
	def got_template (self, f):
		self._binds_when [0] = f
	
	def template_rendered (self, f):
		self._binds_when [1] = f
	
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
	
		for func, name, fuvars, favars, str_rule in self.route_map.values ():			 
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
		
		for p in self.packages:
			url = self.packages [p].build_url (thing, *args, **kargs)
			if url:
				return url
				
	#----------------------------------------------
	# Routing
	#----------------------------------------------						
	def route (self, rule, *t, **k):
		def decorator(f):
			self.add_route (rule, f, *t, **k)
		return decorator
	
	def get_route_map (self):
		return self.route_map
	
	def set_route_map (self, route_map):
		self.route_map = route_map
	
	def mount (self, mount, module, partname = "part"):
		part = getattr (module, partname)
		part.init (module, partname, self.mount_p [:-1] + mount)
		self.packages [id (part)] = part
									
	def try_rule (self, path_info, rulepack):
		rule, (f, n, l, a, s) = rulepack		
		if strutil.is_str_like (rule):
			return None, None
			
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
	
	def add_route (self, rule, func, *t, **k):
		if not rule or rule [0] != "/":
			raise AssertionError ("Url rule should be starts with '/'")
					
		s = rule.find ("/<")
		if s == -1:	
			self.route_map [rule] = (func, func.__name__, func.__code__.co_varnames [1:func.__code__.co_argcount], None, rule)
		else:
			s_rule = rule
			rulenames = []
			for r, n in RX_RULE.findall (rule):
				if n.startswith ("int:"):
					rulenames.append ((n[4:], n[:3]))
					rule = rule.replace (r, "/([0-9]+)")
				elif n.startswith ("float:"):
					rulenames.append ((n[6:], n [:5]))
					rule = rule.replace (r, "/([\.0-9]+)")
				elif n.startswith ("path:"):
					rulenames.append ((n[5:], n [:4]))
					rule = rule.replace (r, "/(.+)")	
				else:
					rulenames.append ((n, "string"))
					rule = rule.replace (r, "/([^/]+)")
			rule = rule.replace (".", "\.") + "$"
			re_rule = re.compile (rule)				
			self.route_map [re_rule] = (func, func.__name__, func.__code__.co_varnames [1:func.__code__.co_argcount], tuple (rulenames), s_rule)
			
	def route_search (self, path_info):
		if path_info + "/" == self.mount_p:
			return self.url_for ("/")
		if not path_info.startswith (self.mount_p):
			raise KeyError
		path_info = "/" + path_info [len (self.mount_p):]
		if path_info in self.route_map:			
			return self.route_map [path_info][0]
		#if path_info [-1] == "/" and path_info [:-1] in self.route_map:
		#	return path_info [:-1]
		if path_info + "/" in self.route_map:
			return self.url_for (path_info + "/")
		raise KeyError
					
	def get_package_method (self, path_info, use_reloader = False):
		# 1st, try find in self
		app, method, kargs, match, matchtype = self, None, {}, None, 0
		try:			
			method = self.route_search (path_info)				
		except KeyError: 
			for rulepack in list(self.route_map.items ()):
				method, kargs = self.try_rule (path_info, rulepack)
				if method: 
					match = rulepack
					break
				matchtype = 2
		else:
			if type (method) is not function: # 301 move								
				return self, method, None, None, 3				
			match = path_info
			matchtype = 1
		
		# 2nd, try find in sub packages
		if method is None and self.packages:
			for pid in list (self.packages.keys ()):
				package = self.packages [pid]
				subapp = getattr (package.module, package.packagename)
				if use_reloader and subapp.is_reloadable ():
					del self.packages [pid]
					args, its_packages = (package.mount_p, package.module, package.packagename), package.packages
					subapp.reload_package ()
					self.mount (*args)
					package.start (self.wasc, self.route, its_packages)
					subapp = getattr (package.module, package.packagename)
					
				app, method, kargs, match, matchtype = subapp.get_package_method (path_info, use_reloader)
				if matchtype == 3:
					return app, method, None, None, 3
				if method:
					break
			
		if not method:
			return None, None, None, None, 0
																	
		return app, [self._binds_request [0], method] + self._binds_request [1:4], kargs, match, matchtype
	
	#----------------------------------------------
	# Starting App
	#----------------------------------------------
	def cleanup (self):
		# initing app & packages		
		self._binds_server [2] and self._binds_server [2] (self.wasc)
		for p in list (self.packages.values ()):
			p.cleanup ()
			
	def start (self, wasc, route, packages = None):
		self.wasc = wasc
		if not route: 
			self.route = "/"
		elif not route.endswith ("/"):			
			self.route = route + "/"
		else:
			self.route = route
		
		if packages is None:
			# initing app & packages
			self._binds_server [0] and self._binds_server [0] (self.wasc)
			for p in list (self.packages.values ()):
				p.start (self.wasc, route)
				
		else:
			self._binds_server [1] and self._binds_server [1] (self.wasc)
			self.packages = packages
