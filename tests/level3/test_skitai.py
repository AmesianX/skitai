import skitai
import os
try:
    import tfserver
except ImportError:
    tfserver = None    
import pytest
try:
    import django
except ImportError:
    django = None 

def test_skitai (app):
    skitai.set_worker_critical_point ()
    
    skitai.deflu ("a", "b")
    assert (skitai.dconf ["models-keys"] == ["a", "b"])
    
    if os.name != "posix":
        return
    
    assert skitai.joinpath ('a', 'b') == "/usr/local/bin/a/b"    
    skitai.mount ("/k", app)
    assert skitai.dconf ['mount']["default"][0][1] == ('/usr/local/bin/pytest', 'app')
    
    skitai.dconf ['mount']["default"] = []
    skitai.mount ("/k2", '/path/app.py', 'app')
    assert skitai.dconf ['mount']["default"][0][1] == ('/path/app.py', 'app')
    
    skitai.dconf ['mount']["default"] = []
    skitai.mount ("/k2", 'path/app.py', 'app')
    assert skitai.dconf ['mount']["default"][0][1] == ('/usr/local/bin/path/app.py', 'app')
    
    if tfserver:
        skitai.dconf ['mount']["default"] = []
        skitai.mount ("/k2", tfserver)
        assert skitai.dconf ['mount']["default"][0][1][0].endswith ('tfserver/export/skitai/__export__.py')
        
        skitai.dconf ['mount']["default"] = []
        skitai.mount ("/k2", (tfserver, "dapp"), "dapp")
        assert skitai.dconf ['mount']["default"][0][1][0].endswith ('tfserver/export/skitai/dapp')
        assert skitai.dconf ['mount']["default"][0][1][1] == "dapp"
        
    skitai.dconf ['mount']["default"] = []
    skitai.mount ("/k2", "X11")
    assert skitai.dconf ['mount']["default"][0][1][0].endswith ('/usr/local/bin/X11')
    
    skitai.dconf ['mount']["default"] = []
    skitai.mount ("/k2", "@X11")
    assert skitai.dconf ['mount']["default"][0][1] == "@X11"
    
    if django:
        skitai.dconf ['mount']["default"] = []
        t = os.path.join (os.path.dirname (__file__), "django_")
        skitai.mount ("/k2", t)
        assert skitai.dconf ['mount']["default"][0][1] == t
        
        skitai.dconf ['mount']["default"] = []
        t = os.path.join (os.path.dirname (__file__), "django_", "wsgi.py")
        skitai.mount ("/k2", t, "application")
        
        t = os.path.join (os.path.dirname (__file__), "django_", "settings.py")
        skitai.alias ("@django", skitai.DJANGO, t)
        assert skitai.dconf ["clusters"]["django"]
        