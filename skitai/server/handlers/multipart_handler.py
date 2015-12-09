from . import ssgi_handler, wsgi_handler
import tempfile
import os

class File:
	def __init__ (self, max_size):
		self.max_size = max_size
		self.descriptor = tempfile.NamedTemporaryFile(delete=False)
		self.size = 0
		
	def write (self, data):
		self.descriptor.write (data)
		self.size += len (data)
		if self.max_size and self.size > self.max_size:
			raise ValueError("file size is over %d MB" % (self.size/1024./1024,))
	
	def close (self):
		self.descriptor.close ()
	
	
class Part:
	def __init__ (self, header, max_size):
		if type (header) is not type ([]):
			header = header.split ("\r\n")
		self.header =	header
		self.max_size = max_size		
		self.value = b""
		self.filename = None
		self.boundary = None
		self.subpart = None		
		
		self.content_type, attr = self.get_header_with_attr ("Content-Type")
		if self.content_type.startswith ("multipart/"):			
			self.boundary = attr ["boundary"].encode ("utf8")
			self.value = []
		
		else:
			val, attr =	self.get_header_with_attr ("Content-Disposition")
			if val:
				self.name = attr ["name"].replace ('"', "")
				if "filename" in attr and attr ["filename"]:
					self.filename = attr ["filename"].replace ('"', "")
					if self.filename:	
						self.value = File (self.max_size)
	
	def get_remote_filename (self):
		return self.filename
		
	def get_local_filename (self):
		return self.value.descriptor.name
	
	def get_file_size (self):
		return self.value.size
	
	def get_content_type (self):
		return self.content_type
				
	def mv (self, to):
		os.rename (self.get_local_filename (), to)
		
	def get_header_with_attr (self, header):
		d = {}
		v = self.get_header (header)
		if v is None:
			return "", d
			
		v2 = v.split (";")
		if len (v2) == 1:
			return v, d
		for each in v2 [1:]:
			try:
				a, b = each.strip ().split ("=", 1)
			except ValueError:
				a, b = each.strip (), None
			d [a] = b
		return v2 [0], d	
			
	def get_header (self, header):
		header = header.lower()	
		h = header + ':'
		hl = len(h)
		for line in self.header:
			if line [:hl].lower() == h:
				r = line [hl:].strip ()
				return r
		return None
		
	def add_new_part (self, part):
		if self.subpart:
			self.subpart.add_new_part (part)
		elif part.is_multipart ():
			self.subpart = part
		else:
			self.value.append (part)
		
	def end_part (self):
		if self.subpart:
			self.value.append (self.subpart)
			self.subpart = None
		
	def get_boundary (self):
		if self.subpart:
			return self.subpart.get_boundary ()
		b = self.boundary
		if b:
			return b"\r\n--" + b
		
	def is_multipart (self):
		return self.boundary
	
	def is_file (self):
		return self.filename
	
	def is_formdata (self):
		return not (self.is_multipart () or self.is_file ())
	
	def collect_incoming_data (self, data):
		if self.filename:
			self.value.write (data)			
		else:
			self.value += data
	
	def end (self):
		if self.filename:
			self.value.close ()			
			
	
class Collector (ssgi_handler.Collector):
	def __init__ (self, handler, request, max_size):
		self.handler = handler
		self.request = request
		self.max_size = max_size
		self.end_of_data = b""
		self.cached = False
		self.cache = []
		self.parts = Part (self.request.header, max_size)
		self.current_part = None
		self.buffer = b""		
		self.content_length = self.get_content_length ()
	
	def get_cache (self):
		if not self.cached:
			return None
		return b"".join (self.cache)
								
	def start_collect (self):						
		if self.content_length == 0: 
			return self.found_terminator()
		
		if self.content_length <= ssgi_handler.MAX_POST_SIZE: #5M
			self.cached = True
									
		self.trackable_tail = None
		self.top_boundary = self.parts.get_boundary ()
		self.request.channel.set_terminator (self.top_boundary [2:]) # exclude \r\n
		
	def collect_incoming_data (self, data):
		#print data
		#print "multipart_handler.collector << %d" % len (data), id (self)
		if self.cached:
			self.cache.append (data)
			
		if self.current_part:
			self.current_part.collect_incoming_data (data)			
		else:	
			self.buffer += data
			if self.buffer == b"--" and self.trackable_tail == self.top_boundary:
				self.stop_collect ()
		
		self.trackable_tail = None		
	
	def abort (self):
		self.buffer = b""
		self.parts = None
		self.cache = []
		self.request.collector = None
		
	def stop_collect (self):
		self.parts.end_part ()
		data = {}
		for part in self.parts.value:
			if part.is_multipart ():
				parts = part.value # some browser, same name-multi value data encode to mutipart/mixed
			else:
				parts = [part]				
				for part in parts:
					if part.is_file ():
						d = {
							"remote": part.get_remote_filename (), 
							"local": part.get_local_filename (),
							"size": part.get_file_size (),
							"mimetype": part.get_content_type ()
						}
					else:
						d = part.value
							
					if part.name in data:
						if type (data [part.name]) is not type ([]):
							data [part.name] = [data [part.name]]						
						data [part.name].append (d)
					else:
						data [part.name] = d
		
		# cached string data if size < 5 MB
		self.request.collector = None # break circ. ref
		self.request.set_body (self.get_cache ())
		self.handler.continue_request (self.request, data)
		self.request.channel.set_terminator (b'\r\n\r\n')
				
	def found_terminator (self):
		c = self.request.channel
		current_terminator = c.get_terminator ()
		
		if self.cached:
			self.cache.append (current_terminator)
			
		if current_terminator == b"\r\n\r\n":
			self.trackable_tail = None
			if not self.buffer:
				return
								
			if self.buffer [:2] == b"--":
				self.parts.end_part ()
				pointer = 4
			else:
				pointer = 2
					
			bl = len (self.buffer)
			while pointer < bl:
				if self.buffer [pointer] not in b"\n\r\t ":
					break
				pointer += 1					
			data, self.buffer = self.buffer [pointer:], b""
									
			p = Part (data.decode ("utf8"), self.max_size)			
			self.parts.add_new_part (p)
			
			if p.is_multipart ():
				self.current_part = None
			else:
				self.current_part = p
				
			c.set_terminator (self.parts.get_boundary ())
			
		else:
			self.trackable_tail = current_terminator
			c.set_terminator (b"\r\n\r\n")
			if self.current_part:
				self.current_part.end ()			
			self.current_part = None
		

class Handler (ssgi_handler.Handler):
	def __init__(self, wasc, max_size = 0):
		self.max_size = max_size
		ssgi_handler.Handler.__init__(self, wasc)		
		
	def match (self, request):
		if request.command != "post":
			return 0
		ct = request.get_header ("content-type")
		if ct and ct.startswith ("multipart/form-data"):
			return 1
		return 0
		
	def handle_request (self, request):
		collector = self.make_collector (Collector, request, ssgi_handler.MAX_UPLOAD_SIZE, self.max_size)
		if collector:
			request.collector = collector
			collector.start_collect ()


class HandlerForWSGI (Handler, wsgi_handler.Handler):
	pass
	
