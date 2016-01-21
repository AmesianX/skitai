from . import response, request
from skitai.protocol.http import request_handler
from skitai.client import asynconnect
from skitai.lib import strutil

try:
	from urllib.parse import urlparse
except ImportError:
	from urlparse import urlparse
from base64 import b64encode
import os
import struct
import sys
try:
	from cStringIO import StringIO as BytesIO
except ImportError:
	from io import BytesIO

FIN    = 0x80
OPCODE = 0x0f
MASKED = 0x80
PAYLOAD_LEN = 0x7f
PAYLOAD_LEN_EXT16 = 0x7e
PAYLOAD_LEN_EXT64 = 0x7f
	
OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xa

	
class RequestHandler (request_handler.RequestHandler):
	def __init__ (self, asyncon, request, callback, *args, **karg):
		request_handler.RequestHandler.__init__ (self, asyncon, request, callback, "1.1", connection = "keep-alive, Upgrade")
		self.initialize ()
	
	def initialize (self):
		self.buf = b""
		self.rfile = BytesIO ()		
		self.opcode = None
		self.payload_length = 0		
		self.has_masks = True
		self.masks = b""
		self.__handshaking = False
		
	def start (self):
		if not self.asyncon.upgraded:
			self.__handshaking = True
			for buf in self.get_request_buffer ():
				self.asyncon.push (buf)				
		else:	
			self.asyncon.push (self.request.get_message ())
			self.asyncon.set_terminator (2)						
		self.asyncon.start_request (self)
		
	def get_request_buffer (self):
		hc = {}		
		scheme, netloc, script, param, queystring, fragment = urlparse (self.request.uri)
		
		addr, port = self.request.get_address ()
		if (scheme == "ws" and port == 80) or (scheme == "wss" and port == 443):
			host = addr [0]
		else:
			host = "%s:%d" % (addr, port)	
					
		hc ['Host'] = host
		hc ['Origin'] = "%s://%s" % (type (self.asyncon) is asynconnect.AsynConnect and "https" or "http", hc ['Host'])
		hc ['Sec-WebSocket-Key'] = b64encode(os.urandom(16))
		hc ['Connection'] = "keep-alive, Upgrade"
		hc ['Upgrade'] = 'websocket'
		hc ['Cache-Control'] = 'no-cache'
		hc ['Pragma'] = 'no-cache'
		
		auth_header = self.get_http_auth_header ()
		if auth_header:
			hc ["Authorization"] = auth_header
		
		uri = self.asyncon.is_proxy () and self.request.uri.replace ("wss://", "https://").replace ("ws://", "http://") or self.request.path
		req = ("GET %s HTTP/1.1\r\n%s\r\n\r\n" % (
			uri,
			"\r\n".join (["%s: %s" % x for x in list(hc.items ())])
		)).encode ("utf8")
		return [req]
	
	def handle_disconnected (self):
		return False
	
	def collect_incoming_data (self, data):
		#print ("+++++++", data)
		if self.__handshaking:
			request_handler.RequestHandler.collect_incoming_data (self, data)
		elif self.masks or (not self.has_masks and self.payload_length):
			self.rfile.write (data)
		else:
			self.buf = data	
		
	def _tobytes (self, b):
		if sys.version_info[0] < 3:
			return map(ord, b)
		else:
			return b
	
	def found_end_of_body (self):
		if self.handled_http_authorization ():					
			return
		
		if not (self.response.code == 101 and self.response.get_header ("Sec-WebSocket-Accept")):
			self.response = response.FailedResponse (self.response.code, self.response.msg)
			self.asyncon.close_it = True
			self.asyncon.handle_close ()
			
		else:
			self.response = None
			self.__handshaking = False
			self.asyncon.upgraded = True
											
			msg = self.request.get_message ()
			print ("&&&&&&&&&&&&&&&&&&&&&&&", str (msg))
			self.asyncon.push (msg)
			self.asyncon.set_terminator (2)
						
	def found_terminator (self):
		if self.__handshaking:
			request_handler.RequestHandler.found_terminator (self)
			
		elif self.masks or not self.has_masks:
			# end of message
			masked_data = bytearray(self.rfile.getvalue ())
			if self.masks:
				masking_key = bytearray(self.masks)
				data = bytearray ([masked_data[i] ^ masking_key [i%4] for i in range (len (masked_data))])
			else:
				data = masked_data	
			
			if self.opcode == OPCODE_TEXT:
				# text
				data = data.decode('utf-8')
			
			self.response = response.Response (200, "OK", self.opcode, data)
			self.asyncon.set_terminator (2)
			self.asyncon.close_it = False
			self.asyncon.handle_close ()
						
		elif self.payload_length:
			self.masks = self.buf
			self.asyncon.set_terminator (self.payload_length)
		
		elif self.opcode:
			if len (self.buf) == 2:
				fmt = ">H"
			else:
				fmt = ">Q"
			self.payload_length = struct.unpack(fmt, self._tobytes(self.buf))[0]
			if self.has_masks:
				self.asyncon.set_terminator (4) # mask
			else:
				self.asyncon.set_terminator (self.payload_length)
		
		elif self.opcode is None:
			b1, b2 = self._tobytes(self.buf)
			fin    = b1 & FIN
			self.opcode = b1 & OPCODE
			if self.opcode == OPCODE_CLOSE:
				self.asyncon.close_it = True
				self.asyncon.handle_close ()
				return
				
			mask = b2 & MASKED
			if not mask:
				self.has_masks = False
			
			payload_length = b2 & PAYLOAD_LEN
			if payload_length == 0:
				self.opcode = None
				self.has_masks = True
				self.asyncon.set_terminator (2)
				self.response = response.Response (self.request, "")				
				self.asyncon.handle_close ()
				return
			
			if payload_length < 126:
				self.payload_length = payload_length
				if self.has_masks:
					self.asyncon.set_terminator (4) # mask
				else:
					self.asyncon.set_terminator (self.payload_length)
			elif payload_length == 126:
				self.asyncon.set_terminator (2)	# short length
			elif payload_length == 127:
				self.asyncon.set_terminator (8) # long length

		else:
			raise AssertionError ("Web socket frame decode error")
					
		