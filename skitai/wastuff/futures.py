import sys
from ..utility import make_pushables
from ..exceptions import HTTPError
from ..rpc.cluster_dist_call import DEFAULT_TIMEOUT

class Tasks:
    def __init__ (self, reqs, timeout = 10):
        assert isinstance (reqs, (list, tuple))        
        self.timeout = timeout        
        self.reqs = reqs
        
    @property
    def results (self):        
        return self.dispatch (self.timeout)
    
    def dispatch (self, timeout = DEFAULT_TIMEOUT, cache = None, cache_if = (200,)):
        return [req.dispatch (timeout or self.timeout, cache, cache_if) for req in self.reqs]
    
    def wait (self, timeout = DEFAULT_TIMEOUT):
        return [req.wait (timeout or self.timeout) for req in self.reqs]
        
        
class Futures (Tasks):
    def __init__ (self, was, reqs, timeout = 10):
        Tasks.__init__ (self, reqs, timeout)
        self._was = was
        self.args = {}
        self.fulfilled = None
        self.responded = 0        
        self.ress = [None] * len (self.reqs)
            
    def then (self, func, **kargs):
        self.args = kargs     
        self.fulfilled = func
        for reqid, req in enumerate (self.reqs):            
            req.set_callback (self._collect, reqid, self.timeout)
        return self
                 
    def _collect (self, res):
        self.responded += 1
        reqid = res.meta ["__reqid"]
        self.ress [reqid] = res
        if self.responded == len (self.reqs):
            if self.fulfilled:             
                self.respond ()
            else:
                self._was.response ("205 No Content", "")
                self._was.response.done ()
            
    def respond (self):
        response = self._was.response         
        try:            
            if self.args:
                content = self.fulfilled (self._was, self.ress, **self.args)
            else:
                content = self.fulfilled (self._was, self.ress)
            will_be_push = make_pushables (response, content)
            content = None
        except MemoryError:
            raise
        except HTTPError as e:
            response.start_response (e.status)
            content = response.build_error_template (e.explain, self._was)
        except:            
            self._was.traceback ()
            response.start_response ("502 Bad Gateway")
            content = response.build_error_template (self._was.app.debug and sys.exc_info () or None, self._was)            
       
        if content:
           will_be_push = make_pushables (response, content)
        
        if will_be_push is None:
            return
           
        for part in will_be_push:
            if len (will_be_push) == 1 and type (part) is bytes and len (response) == 0:
                response.update ("Content-Length", len (part))
            response.push (part)                
        response.done ()
    
