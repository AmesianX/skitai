from . import http_date, producers, utility
from . import compressors
import zlib
import time
import os
import sys
from skitai.lib.reraise import reraise 
from skitai.server.threads import trigger

UNCOMPRESS_MAX = 2048

def catch (htmlformating = 0, exc_info = None):
	if exc_info is None:
		exc_info = sys.exc_info()
	t, v, tb = exc_info
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
		buf = ["<hr><div style='color:#d90000; font-weight: bold; margin-top: 5px;'>%s</div><div style='color:#666666; font-weight: bold;'>%s</div>" % (t.__name__.replace (">", "&gt;").replace ("<", "&lt;"), v)]
		buf.append ("<b>at %s at line %s, %s</b>" % (file, line, function == "?" and "__main__" or "function " + function))
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


class http_response:
	reply_code = 200
	reply_message = ""
	is_sent_response = False
		
	def __init__ (self, request):
		self.request = request
		self.reply_headers = [
			('Server', "sae"),
			('Date', http_date.build_http_date (time.time()))
		]
		self.outgoing = producers.fifo ()
		self.is_done = False
	
	def __len__ (self):
		return len (self.outgoing)
			
	def __setitem__ (self, key, value):
		self.set (key, value)

	def __getitem__ (self, key):
		return self.get (key)
	
	def __delitem__ (self, k):
		self.delete (k)
	
	def has_key (self, key):
		key = key.lower ()
		return key in [x [0].lower () for x in self.reply_headers]		
			
	def set (self, key, value):
		self.reply_headers.append ((key, value))
			
	def get (self, key):
		key = key.lower ()
		for k, v in self.reply_headers:
			if k.lower () == key:
				return v
	get_header = get
			
	def delete (self, key):
		index = 0
		found = 0
		key = key.lower ()
		for hk, hv in self.reply_headers:
			if key == hk.lower ():
				found = 1
				break
			index += 1
		
		if found:
			del self.reply_headers [index]
			self.delete (key)
		
	def update (self, key, value):
		self.delete (key)
		self.set (key, value)
	
	def build_reply_header (self):		
		return '\r\n'.join (
				[self.response(self.reply_code, self.reply_message)] + ['%s: %s' % x for x in self.reply_headers]
				) + '\r\n\r\n'			
	
	def get_status_msg (self, code):
		try:
			status = self.responses [code]
		except KeyError: 
			status = "Undefined"
		return status	
	
	def response (self, code, status):	
		return 'HTTP/%s %d %s' % (self.request.version, code, status)
	
	def instant (self, code, status = "", headers = None):
		# instance messaging
		self.reply_code = code
		if status: self.reply_message = status
		else:	self.reply_message = self.get_status_msg (code)
				
		reply = [self.response (self.reply_code, self.reply_message)]
		if headers:
			for header in headers:
				reply.append ("%s: %s" % header)
		self.request.channel.push (("\r\n".join (reply) + "\r\n\r\n").encode ("utf8"))
	
	def abort (self, code, status = "", why = ""):
		self.request.channel.reject ()		
		self.error (code, status, why, force_close = True)
	
	def responsable (self):
		if self.is_done: return False
		if self.request.channel is None: return False
		return True
				
	def start_response (self, status, headers = None, exc_info = None):
		if not self.responsable ():
			if exc_info:
				try:
					reraise (*exc_info)
				finally:
					exc_info = None	
			else:
				raise AssertionError ("Relponse already sent!")		
			return
			
		code, status = status.split (" ", 1)		
		if exc_info:
			self.error (int (code), status, exc_info)
		else:
			self.start (int (code), status, headers)			
			return self.push #by WSGI Spec.
		
	def start (self, code, status = "", headers = None):
		if not self.responsable (): return
		self.reply_code = code
		if status: self.reply_message = status
		else:	self.reply_message = self.get_status_msg (code)
			
		if headers:
			for k, v in headers:
				self.set (k, v)
	reply = start
	
	def error (self, code, status = "", why = "", force_close = False):
		if not self.responsable (): return
		self.reply_code = code
		if status: self.reply_message = status
		else:	self.reply_message = self.get_status_msg (code)
			
		if type (why) is tuple: # sys.exc_info ()
			why = catch (1, why)
		
		s = self.DEFAULT_ERROR_MESSAGE % {
			'code': self.reply_code,
			'message': self.reply_message,
			'info': why,
			'gentime': http_date.build_http_date (time.time ()),
			'url': "http://%s%s" % (self.request.get_header ("host"), self.request.uri)
			}

		self.update ('Content-Length', len(s))
		self.update ('Content-Type', 'text/html')
		self.delete ('content-encoding')
		self.delete ('expires')
		self.delete ('cache-control')
		self.delete ('set-cookie')

		self.push (s.encode ("utf8"))
		self.done (True, True, force_close)
	
	def push (self, thing):
		if not self.responsable (): return
		if type(thing) is bytes:			
			self.outgoing.push (producers.simple_producer (thing))
		else:
			self.outgoing.push (thing)
				
	def done (self, globbing = True, compress = True, force_close = False):
		if not self.responsable (): return
		self.is_done = True
				
		connection = utility.get_header (utility.CONNECTION, self.request.header).lower()
		close_it = False
		way_to_compress = ""
		wrap_in_chunking = False
		
		if force_close:
			close_it = True
		
		else:
			if self.request.version == '1.0':
				if connection == 'keep-alive':
					if 'Content-Length' not in self:
						close_it = True
						self.update ('Connection', 'close')
					else:
						self.update ('Connection', 'keep-alive')
				else:
					close_it = True
			
			elif self.request.version == '1.1':
				if connection == 'close':
					close_it = True
					self.update ('Connection', 'close')
				elif not self.has_key ('Content-Length'):
					wrap_in_chunking = True
					
			else:
				self.update ('Connection', 'close')
				close_it = True
		
		if compress and not self.has_key ('Content-Encoding'):
			maybe_compress = self.request.get_header ("Accept-Encoding")
			if maybe_compress and self.has_key ("Content-Length") and int (self ["Content-Length"]) <= UNCOMPRESS_MAX:
				maybe_compress = ""
			
			else:	
				content_type = self ["Content-Type"]
				if maybe_compress and content_type and (content_type.startswith ("text/") or content_type.endswith ("/json-rpc")):
					accept_encoding = [x.strip () for x in maybe_compress.split (",")]
					if "gzip" in accept_encoding:
						way_to_compress = "gzip"
					elif "deflate" in accept_encoding:
						way_to_compress = "deflate"
		
			if way_to_compress:
				if self.has_key ('Content-Length'):
					self.delete ("Content-Length") # rebuild
					wrap_in_chunking = True
				self.update ('Content-Encoding', way_to_compress)
		
		if len (self.outgoing) == 0:
			self.delete ('Transfer-Encoding')
			self.delete ('Content-Length')			
			self.outgoing.push_front (producers.simple_producer (self.build_reply_header().encode ("utf8")))
			outgoing_producer = producers.composite_producer (self.outgoing)
			
		else:	
			if wrap_in_chunking:
				self.delete ('Content-Length')
				self.update ('Transfer-Encoding', 'chunked')
				
				if way_to_compress:
					if way_to_compress == "gzip": 
						producer = producers.gzipped_producer
					else: # deflate
						producer = producers.compressed_producer
					outgoing_producer = producer (producers.composite_producer (self.outgoing))
					
				else:
					outgoing_producer = producers.composite_producer (self.outgoing)				
					
				outgoing_producer = producers.chunked_producer (outgoing_producer)
				outgoing_header = producers.simple_producer (self.build_reply_header().encode ("utf8"))
				outgoing_producer = producers.composite_producer (
					producers.fifo([outgoing_header, outgoing_producer])
				)
				
			else:
				self.delete ('Transfer-Encoding')				
				if way_to_compress:
					if way_to_compress == "gzip":
						compressor = compressors.GZipCompressor ()
					else: # deflate
						compressor = zlib.compressobj (6, zlib.DEFLATED)
					
					cdata = ""
					has_producer = 1
					while 1:
						has_producer, producer = self.outgoing.pop ()
						if not has_producer: break
						cdata += compressor.compress (producer.data)				
					cdata += compressor.flush ()
					
					self.update ("Content-Length", len (cdata))
					self.outgoing = producers.fifo ([producers.simple_producer (cdata)])
				
				outgoing_header = producers.simple_producer (self.build_reply_header().encode ("utf8"))
				self.outgoing.push_front (outgoing_header)
				outgoing_producer = producers.composite_producer (self.outgoing)
		
		try:
			if globbing:				
				self.request.channel.push_with_producer (producers.globbing_producer (producers.hooked_producer (outgoing_producer, self.log)))
			else:
				self.request.channel.push_with_producer (producers.hooked_producer (outgoing_producer, self.log))
			
			self.request.channel.current_request = None
			# proxy collector and producer is related to asynconnect
			# and relay data with channel
			# then if request is suddenly stopped, make sure close them
			self.request.channel.close_when_close ([self.request.collector, self.request.producer])
			if close_it:
				self.request.channel.close_when_done()
		
		except:
			self.request.logger.trace ()
			self.request.logger.log (
				'channel maybe closed',
				'warning'
			)		
	
	def log (self, bytes):		
		self.request.channel.server.log_request (
			'%s:%d %s %s %d'
			% (self.request.channel.addr[0],
			self.request.channel.addr[1],			
			self.request.request,
			self.reply_code,			
			bytes)
			)
	
		
	# Default error message
	DEFAULT_ERROR_MESSAGE = """
<!DOCTYPE html>
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>ERROR: %(code)d %(message)s</title>
<style type="text/css"><!-- * {font-family: verdana, sans-serif;}html body {margin: 0;padding: 0;background: #efefef;font-size: 12px;color: #1e1e1e;}#titles {margin-left: 15px;padding: 10px;}#titles h1 {color: #000000;}#titles h2 {color: #000000;} #content {padding: 10px;background: #ffffff;}p {}#error p, h3, b { font-size: 11px;}#error{margin: 0; padding: 0;} hr {margin:0; padding:0;} #error hr {border-top:#888888 1px solid;} #error li, i { font-weight: normal;}#footer {font-size: 9px;padding-left: 10px;}body :lang(fa) { direction: rtl; font-size: 100%%; font-family: Tahoma, Roya, sans-serif; float: right; } :lang(he) { direction: rtl; } --></style>
</head>
<body>
<div id="titles"><h1>ERROR</h1><h2>%(code)d %(message)s</h2></div>
<hr />
<div id="content">
<p>The following error was encountered while trying to retrieve the URL:
<a href="%(url)s">%(url)s</a></p> 
<div id="error"><p><b>%(code)d %(message)s</p><p>%(info)s</p></div><br />
</div> 
<hr />
<div id="footer">
<p>Generated %(gentime)s</p>
</div>
</body>
</html>"""

	responses = {
		100: "Continue",
		101: "Switching Protocols",
		200: "OK",
		201: "Created",
		202: "Accepted",
		203: "Non-Authoritative Information",
		204: "No Content",
		205: "Reset Content",
		206: "Partial Content",
		300: "Multiple Choices",
		301: "Moved Permanently",
		302: "Moved Temporarily",
		303: "See Other",
		304: "Not Modified",
		305: "Use Proxy",
		400: "Bad Request",
		401: "Unauthorized",
		402: "Payment Required",
		403: "Forbidden",
		404: "Not Found",
		405: "Method Not Allowed",
		406: "Not Acceptable",
		407: "Proxy Authentication Required",
		408: "Request Time-out",
		409: "Conflict",
		410: "Gone",
		411: "Length Required",			
		412: "Precondition Failed",
		413: "Request Entity Too Large",
		414: "Request-URI Too Large",
		415: "Unsupported Media Type",
		500: "Internal Server Error",
		501: "Not Implemented",
		502: "Bad Gateway",
		503: "Service Unavailable",
		504: "Gateway Time-out",
		505: "HTTP Version Not Supported",
		506: "Proxy Error",
		507: "Failed Establishing Connection",
	}		
		
