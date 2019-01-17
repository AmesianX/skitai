from atila import Atila
import confutil
import skitai
import asyncore

def test_cli (app):
    @app.route ("/")
    def index (was):
        return "Hello, World"
    
    @app.route ("/echo")
    def echo (was, m):
        return m
    
    @app.route ("/json")
    def json (was, m):
        return was.response.api (data = m)
    
    app.alias ("@pypi", skitai.PROTO_HTTP, "aw3.skitai.com")    
    with app.make_client ("/", confutil.getroot ()) as cli:
        resp = cli.get ("/")
        assert resp.text == "Hello, World"

        resp = cli.get ("/echo?m=GET")
        assert resp.text == "GET"
        
        resp = cli.post ("/json", {"m": "POST"})
        assert resp.text == '{"data": "POST"}'
        
        resp = cli.postjson ("/json", {"m": "POST"})
        assert resp.text == '{"data": "POST"}'
        