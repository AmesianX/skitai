import zlib
import time
import os
import sys
from aquests.protocols.http import http_date, http_util
from rs4.reraise import reraise 
from rs4 import producers, compressors
from aquests.protocols.http import respcodes
from .wastuff import selective_logger
from .utility import catch
import skitai
import asyncore
import json
from skitai import exceptions
from .wastuff.api import API, catch

try: 
    from urllib.parse import urljoin
except ImportError:
    from urlparse import urljoin    


UNCOMPRESS_MAX = 2048
ONETIME_COMPRESS_MAX = 1048576

# Default error message
DEFAULT_ERROR_MESSAGE = """<!DOCTYPE html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>%(code)d %(message)s</title>
<style type="text/css"><!-- *{font-family:sans-serif, verdana;}body{margin:0;padding:0;font-size:14px;color:#1e1e1e;font-family:verdana,sans-serif;text-align:center;} #titles{padding:16px;}#titles h1{color: #000000;} #content{padding:16px 16px 30px 16px;} #debug h2 {font-size: 18px; font-weight: 600;} #debug h3{font-size: 16px; color: #d90000;} #debug p,b,h4,li {font-size:14px;}#debug h4{color: #999999;} #debug li{margin-bottom: 6px;} #debug .f {color:#8AB088; font-weight: bold;} #debug .n {color:#003366;font-weight:bold;} #debug{text-align: left; background-color: #FFF; margin-bottom: 32px; padding:8px 16px 8px 16px; border-radius: 3px;} #debug li,i{font-weight:normal;}#footer {font-size:12px;padding-left:10px;}#software {border-top: solid 1px #cccccc;} #software div {padding-top: 10px; font-size: 12px;} --></style>
</head>
<body>
<div id="titles"><h1>%(code)d %(message)s</h1></div>
<div id="content">
<div id="detail">%(detail)s</div>
<div id="traceback">
    <div id="%(mode)s"><p>%(traceback)s</p></div>
</div>
<div id="software"><div>%(software)s</div></div>
</div>
</body>
</html>"""


class http_response:
    log_or_not = selective_logger.SelectiveLogger ()
    reply_code = 200
    reply_message = "OK"
    _is_async_streaming = False
        
    def __init__ (self, request):
        self.request = request
        self.reply_headers = [
            ('Server', skitai.NAME),
            ('Date', http_date.build_http_date (time.time()))
        ]
        self.outgoing = producers.fifo ()
        self._is_done = False
        self.stime = time.time ()
        self.htime = 0
        self.content_type = None
        self.current_app = None
        self.current_env = None
        
    def is_async_streaming (self):
        return self._is_async_streaming
    
    def set_streaming (self):
        self._is_async_streaming = True    
    
    def is_responsable (self):
        return not self._is_done
    
    def is_done (self):    
        return self._is_done
                
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
    
    def append_header (self, key, value):
        val = self.get (key)
        if not val:
            self.set (key, value)
        else:
            self.set (key, val + ", " + value)
    
    set_header = set
    get_header = get
    del_header = delete    
    
    def set_cache (self, max_age = 0):
        if self.request.version == "1.0":            
            self.set_header ('Expires', http_date.build_http_date (time.time() + max_age))                
        else:
            self.set_header ('Cache-Control', "max-age=%d" % max_age)
                    
    def set_headers (self, headers):
        for k, v in headers:
            self.set (k, v)
    
    def append_headers (self, headers):
        for k, v in headers:
            self.set (k, v)        
            
    def get_hedaers (self):    
        return self.reply_headers
    
    def set_reply (self, status):
        # for atila.Atila App
        self.reply_code, self.reply_message = self.parse_ststus (status)    
    set_status = set_reply
        
    def get_status (self):
        return "%d %s" % self.get_reply ()
    
    def build_reply_header (self, with_header = 1):
        h = [self.response (self.reply_code, self.reply_message)]
        if with_header:
            h.extend (['%s: %s' % x for x in self.reply_headers])
        h = '\r\n'.join (h) + '\r\n\r\n'            
        return h        
    
    def get_status_msg (self, code):
        return respcodes.get (code, "Undefined Error")
        
    def response (self, code, status):    
        return 'HTTP/%s %d %s' % (self.request.version, code, status)
    
    def parse_ststus (self, status):
        try:    
            code, status = status.split (" ", 1)
            code = int (code)
        except:
            raise AssertionError ("Can't understand given status code")        
        return code, status    
    
    def get_reply (self):
        # for atila.Atila App
        return self.reply_code, self.reply_message
    
    def instant (self, status = "", headers = None):
        # instance messaging        
        if self.request.channel is None:
            return
        code, msg = self.parse_ststus (status)
        reply = [self.response (code, msg)]
        if headers:
            for header in headers:
                reply.append ("%s: %s" % header)
        self.request.channel.push (("\r\n".join (reply) + "\r\n\r\n").encode ("utf8"))
    
    # for WSGI App -------------------------------------------------------    
    def start_response (self, status, headers = None, exc_info = None):
        if not self.is_responsable ():
            if exc_info:
                try:
                    reraise (*exc_info)
                finally:
                    exc_info = None    
            else:
                raise AssertionError ("Response already sent!")        
            return
        
        code, status = self.parse_ststus (status)
        self.start (code, status, headers)
        
        if exc_info:
            # expect plain/text, send exception info to developers
            content = catch (0, exc_info)
            self.push (content)
            
        return self.push #by WSGI Spec.
    
    # Internal Response Methods ------------------------------------------        
    def abort (self, code, status = "", why = ""):
        self.request.channel.reject ()
        self.error (code, status, why, force_close = True)
        
    def start (self, code, status = "", headers = None):
        if not self.is_responsable (): return
        self.reply_code = code
        if status: self.reply_message = status
        else:    self.reply_message = self.get_status_msg (code)            
        if headers:
            for k, v in headers:
                self.set (k, v)
    reply = start
    
    def catch (self, exc_info = None):
        if self.request.get_header ('accept').find ("application/json") != -1:
            return catch (2, exc_info)
        return catch (1, exc_info)
        
    def build_error_template (self, why = '', was = None):
        global DEFAULT_ERROR_MESSAGE
        
        exc_info = None
        if type (why) is tuple: # sys.exc_info ()
            if self.current_app and not self.current_app.debug:
                why = None
            exc_info, why  = why, ''
        
        error = {}
        if self.request.get_header ('accept', '').find ("application/json") != -1:            
            message = why or self.reply_message.lower ()
            debug = None
            if exc_info:
                debug = 'see traceback' 
            return self.fault (message, 0, debug, exc_info = exc_info)
        
        else:    
            self.update ('content-type', 'text/html')
            error ['detail'] = why
            error ['time'] = http_date.build_http_date (time.time ())
            error ['url'] = urljoin ("%s://%s/" % (self.request.get_scheme (), self.request.get_header ("host")), self.request.uri)
            error ['software'] = skitai.NAME            
            error ['mode'] = exc_info and 'debug' or 'normal'                
            error ['code'] = self.reply_code
            error ['message'] = self.reply_message
            error ["traceback"] = exc_info and catch (1, exc_info) or None
            
            content = None    
            if self.current_app and hasattr (self.current_app, 'render_error'):
                try:
                    content = self.current_app.render_error (error, was)
                except:                            
                    self.request.logger.trace ()                
                    if self.current_app.debug:                        
                        error ["traceback"] += "<h2 style='padding-top: 40px;'>Exception Occured During Building Error Template</h2>" + catch (1)
                        
            return content or (DEFAULT_ERROR_MESSAGE % error)
                
    def error (self, code, status = "", why = "", force_close = False, push_only = False):
        if not self.is_responsable (): return
        self.outgoing.clear ()
        self.reply_code = code
        if status: self.reply_message = status
        else:    self.reply_message = self.get_status_msg (code)
        
        body = self.build_error_template (why).encode ("utf8")
        self.update ('Content-Length', len(body))                
        self.update ('Cache-Control', 'max-age=0')
        self.push (body)
        if not push_only:
            self.done (force_close)
                
    # Send Response -------------------------------------------------------        
    def die_with (self, thing):
        if self.request.channel:
            self.request.channel.attend_to (thing)
    
    def hint_promise (self, *args, **kargs):
        # ignore in version 1.x
        pass
    
    def push_and_done (self, thing, *args, **kargs):
        self.push (thing)
        self.done (*args, **kargs)
            
    def push (self, thing):        
        if not self.is_responsable (): 
            return
        if type(thing) is bytes:
            self.outgoing.push (producers.simple_producer (thing))
        else:            
            self.outgoing.push (thing)
    
    def __call__ (self, status = "200 OK", body = None, headers = None, exc_info = None):
        self.start_response (status, headers)
        if body is None:            
            return self.build_error_template (exc_info)
        return body
                                
    def done (self, force_close = False, upgrade_to = None, with_header = 1):
        self.content_type = self.get ('content-type')
                
        if not self.is_responsable (): return
        self._is_done = True
        if self.request.channel is None: return
            
        self.htime = (time.time () - self.stime) * 1000
        self.stime = time.time () #for delivery time
        
        # compress payload and globbing production
        do_optimize = True
        if upgrade_to or self.is_async_streaming ():
            do_optimize = False
                        
        connection = http_util.get_header (http_util.CONNECTION, self.request.header).lower()
        close_it = False
        way_to_compress = ""
        wrap_in_chunking = False
        
        if force_close:
            close_it = True
            if self.request.version == '1.1':
                self.update ('Connection', 'close')
            else:    
                self.delete ('Connection')
                
        else:
            if self.request.version == '1.0':
                if connection == 'keep-alive':
                    if not self.has_key ('content-length'):
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
                if not self.has_key ('transfer-encoding') and not self.has_key ('content-length') and self.has_key ('content-type'):
                    wrap_in_chunking = True
                    
            else:
                # unknown close
                self.update ('Connection', 'close')
                close_it = True
        
        if len (self.outgoing) == 0:
            self.update ('Content-Length', "0")
            self.delete ('transfer-encoding')            
            self.delete ('content-type')
            outgoing_producer = producers.simple_producer (self.build_reply_header(with_header).encode ("utf8"))
            do_optimize = False
            
        elif len (self.outgoing) == 1 and hasattr (self.outgoing.first (), "ready"):
            outgoing_producer = producers.composite_producer (self.outgoing)
            if wrap_in_chunking:
                self.update ('Transfer-Encoding', 'chunked')
                outgoing_producer = producers.chunked_producer (outgoing_producer)
            outgoing_header = producers.simple_producer (self.build_reply_header(with_header).encode ("utf8"))
            self.request.channel.push_with_producer (outgoing_header)
            do_optimize = False
            
        elif do_optimize and not self.has_key ('Content-Encoding'):
            maybe_compress = self.request.get_header ("Accept-Encoding")
            if maybe_compress:
                cl = self.has_key ("content-length") and int (self.get ("Content-Length")) or -1
                if cl == -1:
                    cl = self.outgoing.get_estimate_content_length ()
                    
                if 0 < cl  <= UNCOMPRESS_MAX:
                    maybe_compress = ""
                elif not wrap_in_chunking and cl > ONETIME_COMPRESS_MAX:
                    # too big for memory, do not compress
                    maybe_compress = ""
            
            if maybe_compress:
                content_type = self.get ("Content-Type")
                if content_type and (content_type.startswith ("text/") or content_type.startswith ("application/json")):
                    accept_encoding = [x.strip () for x in maybe_compress.split (",")]
                    if "gzip" in accept_encoding:
                        way_to_compress = "gzip"
                    elif "deflate" in accept_encoding:
                        way_to_compress = "deflate"
            
            if way_to_compress:
                if self.has_key ('Content-Length'):
                    self.delete ("content-length") # rebuild
                self.update ('Content-Encoding', way_to_compress)    
            
            if wrap_in_chunking:
                outgoing_producer = producers.composite_producer (self.outgoing)
                self.delete ('content-length')
                self.update ('Transfer-Encoding', 'chunked')                
                if way_to_compress:
                    if way_to_compress == "gzip": 
                        compressing_producer = producers.gzipped_producer
                    else: # deflate
                        compressing_producer = producers.compressed_producer
                    outgoing_producer = compressing_producer (outgoing_producer)                    
                outgoing_producer = producers.chunked_producer (outgoing_producer)
                outgoing_header = producers.simple_producer (self.build_reply_header(with_header).encode ("utf8"))                
                
            else:
                self.delete ('transfer-encoding')
                if way_to_compress:                    
                    if way_to_compress == "gzip":
                        compressor = compressors.GZipCompressor ()
                    else: # deflate
                        compressor = zlib.compressobj (6, zlib.DEFLATED)
                    cdata = b""
                    has_producer = 1
                    while 1:
                        has_producer, producer = self.outgoing.pop ()
                        if not has_producer: break
                        while 1:    
                            data = producer.more ()
                            if not data:
                                break
                            cdata += compressor.compress (data)
                    cdata += compressor.flush ()                    
                    self.update ("Content-Length", len (cdata))
                    outgoing_producer = producers.simple_producer (cdata)                        
                else:
                    outgoing_producer = producers.composite_producer (self.outgoing)
                    
                outgoing_header = producers.simple_producer (self.build_reply_header(with_header).encode ("utf8"))                
            
            outgoing_producer = producers.composite_producer (
                producers.fifo([outgoing_header, outgoing_producer])
            )

        outgoing_producer = self.log_or_not (self.request.uri, outgoing_producer, self.log)                
        if do_optimize:
            outgoing_producer = producers.globbing_producer (outgoing_producer)
        
        # IMP: second testing after push_with_producer()->init_send ()
        if self.request.channel is None: return

        if upgrade_to:
            request, terminator = upgrade_to
            self.request.channel.current_request = request
            self.request.channel.set_terminator (terminator)
        else:
            # preapre to receice new request for channel
            self.request.channel.current_request = None
            self.request.channel.set_terminator (b"\r\n\r\n")
        
        # proxy collector and producer is related to asynconnect
        # and relay data with channel
        # then if request is suddenly stopped, make sure close them
        self.die_with (self.request.collector)
        self.die_with (self.request.producer)
        
        logger = self.request.logger #IMP: for  disconnect with request
        try:
            if outgoing_producer:
                self.request.channel.push_with_producer (outgoing_producer)
            if close_it:
                self.request.channel.close_when_done ()
        except:            
            logger.trace ()
                        
    def log (self, bytes):
        server = self.request.channel.server        
        referer = self.request.get_header ('referer')
        real_ip = self.request.get_real_ip ()
        worker_id = server.worker_ident [8:]
        worker = worker_id and ("W" + worker_id) or "M"
            
        server.log_request (
            '%s %s %s %s %s %d %s %d %s %s %s %s %s %s %s %s %d %d %d'
            % (
            self.request.channel.addr[0],
            
            self.request.host or "-",
            self.request.is_promise () and "PUSH" or self.request.method,
            self.request.uri,
            self.request.version,
            self.request.rbytes, # recv body size            
            
            self.reply_code,            
            bytes, # send body size
            
            self.request.gtxid or "-",
            self.request.ltxid or "-",
            self.request.user and '"' + str (self.request.user) + '"' or "-",
            self.request.token or "-",
            
            referer and '"' + referer + '"' or "-",
            self.request.user_agent and '"' + self.request.user_agent + '"' or "-",
            real_ip != self.request.channel.addr[0] and real_ip or '-',
            
            worker,
            len (asyncore.socket_map),
            self.htime, # due time to request handling
            (time.time () - self.stime) * 1000, # due time to sending data            
            )
        )
        # clearing resources, back refs
        self.request.response_finished ()

    
    # Sugar syntaxes ----------------------------------------------------------------    
    def adaptive_error (self, status, message, code, more_info):
        ac = self.request.get_header ('accept', '')
        if ac.find ("text/html") != -1:
            return self.with_explain (status, "{} (code: {}): {}".format (message, code, more_info))
        return self.Fault (sttus, status, message, code, None, more_info)
    
    def fault (self, message = "", code = 0,  debug = None, more_info = None, exc_info = None, traceback = False):
        api = self.api ()
        if not code:
            code = int (self.reply_code) * 100 + (traceback and 90 or 0)
        if traceback:
            api.traceback (message, code, debug or "see traceback", more_info)
        else:    
            api.error (message, code, debug, more_info, exc_info)
        self.update ("Content-Type", api.get_content_type ())            
        return api
    eapi = fault # will be derecating
    
    def throw (self, status, why = ""):
        raise exceptions.HTTPError (status, why)
    
    def with_explain (self, status = "200 OK", why = "", headers = None):
        self.start_response (status, headers)
        return self.build_error_template (why)
    
    def API (self, __data_dict__ = None, **kargs):
        if isinstance (__data_dict__, str):
            self.set_status (__data_dict__)
            __data_dict__ = None
        api = API (self.request, __data_dict__ or kargs)
        self.update ("Content-Type", api.get_content_type ())
        return api
    api = API
        
    def Fault (self, status = "200 OK", *args, **kargs):
        self.set_status (status)
        r = self.fault (*args, **kargs)        
        return self (status, r)
    for_api = Fault
    
    def File (self, path, mimetype = 'application/octet-stream', filename = None):
        self.set_header ('Content-Type',  mimetype)
        self.set_header ('Content-Length', str (os.path.getsize (path)))
        if filename:
            self.set_header ('Content-Disposition', 'attachment; filename="{}"'.format (filename))
        return producers.file_producer (open (path, "rb"))                    
    file = File
        