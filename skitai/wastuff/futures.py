import sys
from ..utility import make_pushables
from ..exceptions import HTTPError
from ..rpc.cluster_dist_call import DEFAULT_TIMEOUT

class TaskBase:
    def __init__ (self, reqs, timeout = DEFAULT_TIMEOUT, cache = 0, cache_if = (200,)):
        assert isinstance (reqs, (list, tuple))        
        self.timeout = timeout        
        self.cache = cache
        self.cache_if = cache_if
        self.reqs = reqs        


class Tasks (TaskBase):
    def __init__ (self, reqs, timeout = DEFAULT_TIMEOUT, cache = 0, cache_if = (200,)):
        TaskBase.__init__ (self, reqs, timeout, cache, cache_if)
        self._results = []
        
    def __iter__ (self):
        return iter (self.results)
    
    def __getitem__ (self, sliced):
        return self.results [sliced]
                
    @property
    def results (self):       
        return self._results or self.dispatch ()
    
    def dispatch (self):
        self._results = [req.dispatch (self.timeout, self.cache, self.cache_if) for req in self.reqs]
        return self._results 
    
    def wait (self):
        self._results = [req.wait (self.timeout) for req in self.reqs]
        
        
class Futures (TaskBase):
    def __init__ (self, was, reqs, timeout = 10, cache = 0, cache_if = (200,)):
        TaskBase.__init__ (self, reqs, timeout, cache, cache_if)
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
        self.cache and res.cache (self.cache, self.cache_if)
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
    
