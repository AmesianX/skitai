from skitai.saddle import launch

def test_launch ():
    with launch ("./examples/app.py", "http://127.0.0.1:30371") as engine:
        api = engine.api
        requests = engine.requests
        
        resp = requests.get ("/")
        assert resp.text.find ("Copyright (c) 2015-present, Hans Roh") > 0
        