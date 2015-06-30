import re, filesys, stat, mime_type_table, os
from skitai.server import http_date, producers
from skitai.server.utility import *

IF_MODIFIED_SINCE = re.compile (
	'If-Modified-Since: ([^;]+)((; length=([0-9]+)$)|$)',
	re.IGNORECASE
	)

USER_AGENT = re.compile ('User-Agent: (.*)', re.IGNORECASE)

CONTENT_TYPE = re.compile (
	r"Content-Type: ([^;]+)((; boundary=([A-Za-z0-9'\(\)+_,./:=?-]+)$)|$)",
	re.IGNORECASE
	)

class Handler:
	directory_defaults = [
		'index.htm',
		'index.html'
		]
		
	def __init__ (self, wasc, pathmap = {}, max_age = 300, alt_handlers = []):
		self.wasc = wasc
		self.max_age = max_age
		self.alt_handlers = alt_handlers
		self.permit_cache = {}
		self.filesystem = filesys.mapped_filesystem ()
		for k, v in pathmap.items ():
			self.add_route (k, v)
				
	def match (self, request):
		return 1
	
	def add_route (self, alias, option):
		self.filesystem.add_map (alias, option)
	
	def handle_alternative (self, request):
		for h in self.alt_handlers:
			if h.match (request):
				try:
					h.handle_request (request)
				except:						
					self.wasc.logger.trace("server")
					try: request.error (500)
					except: pass
				return
		request.error (404)
		
	def isprohibited (self, request, path):
		dirname = os.path.split(path) [0]				
		permission = self.filesystem.get_permission (dirname)
		if not permission:
			return False								
		if not self.wasc.authorizer.has_permission (request, permission):
			return True			
		return False	
		
	def handle_request (self, request):
		if request.command not in ('get', 'head'):
			self.handle_alternative (request)
			return
			
		path, params, query, fragment = request.split_uri()
		if '%' in path:
			path = unquote (path)
			
		while path and path[0] == '/':
			path = path[1:]
		
		if self.filesystem.isdir (path):
			if path and path[-1] != '/':
				request['Location'] = '/%s/' % path
				request.error (301)
				return

			found = False
			if path and path[-1] != '/':
				path = path + '/'
			
			for default in self.directory_defaults:
				p = path + default
				if self.filesystem.isfile (p):
					path = p
					found = True
					break
			
			if not found:
				self.handle_alternative (request)
				return
		
		elif not self.filesystem.isfile (path):
			self.handle_alternative (request)
			return
		
		if self.isprohibited (request, path):
			return
		
		file_length = self.filesystem.stat (path)[stat.ST_SIZE]
		ims = get_header_match (IF_MODIFIED_SINCE, request.header)
		length_match = 1
		if ims:
			length = ims.group (4)
			if length:
				try:
					length = int(length)
					if length != file_length:
						length_match = 0
				except:
					pass
					
		ims_date = 0
		if ims:
			ims_date = http_date.parse_http_date (ims.group (1))
		try:
			mtime = self.filesystem.stat (path)[stat.ST_MTIME]
		except:
			self.handle_alternative (request)
			return

		if length_match and ims_date:
			if mtime <= ims_date:
				request.reply_code = 304
				request.done()				
				return
		try:
			file = self.filesystem.open (path, 'rb')
		except IOError:
			self.handle_alternative (request)
			return

		request['Last-Modified'] = http_date.build_http_date (mtime)
		request['Content-Length'] = file_length
		request['Cache-Control'] = "max-age=%d" % self.max_age
		self.set_content_type (path, request)

		if request.command == 'get':
			request.push (producers.file_producer (file))
			
		request.done()

	def set_content_type (self, path, request):
		ext = get_extension (path).lower()
		if mime_type_table.content_type_map.has_key (ext):
			request['Content-Type'] = mime_type_table.content_type_map[ext]
		else:
			request['Content-Type'] = 'text/plain'
			