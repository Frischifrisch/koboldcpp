# A hacky little script from Concedo that exposes llama.cpp function bindings 
# allowing it to be used via a simulated kobold api endpoint
# it's not very usable as there is a fundamental flaw with llama.cpp 
# which causes generation delay to scale linearly with original prompt length.

import ctypes
import os
import argparse
import json, http.server, threading, socket, sys, time

class load_model_inputs(ctypes.Structure):
    _fields_ = [("threads", ctypes.c_int),
                ("max_context_length", ctypes.c_int),
                ("batch_size", ctypes.c_int),
                ("f16_kv", ctypes.c_bool),
                ("model_filename", ctypes.c_char_p),
                ("n_parts_overwrite", ctypes.c_int)]

class generation_inputs(ctypes.Structure):
    _fields_ = [("seed", ctypes.c_int),
                ("prompt", ctypes.c_char_p),
                ("max_context_length", ctypes.c_int),
                ("max_length", ctypes.c_int),
                ("temperature", ctypes.c_float),
                ("top_k", ctypes.c_int),
                ("top_p", ctypes.c_float),
                ("rep_pen", ctypes.c_float),
                ("rep_pen_range", ctypes.c_int)]

class generation_outputs(ctypes.Structure):
    _fields_ = [("status", ctypes.c_int),
                ("text", ctypes.c_char * 16384)]

handle = None
use_blas = False # if true, uses OpenBLAS for acceleration. libopenblas.dll must exist in the same dir.

def init_library():
    global handle, use_blas
    libname = ""
    if use_blas:
        libname = "koboldcpp_blas.dll"
    else:
        libname = "koboldcpp.dll"

    print("Initializing dynamic library: " + libname)
    dir_path = os.path.dirname(os.path.realpath(__file__))  

    #OpenBLAS should provide about a 2x speedup on prompt ingestion if compatible.
    handle = ctypes.CDLL(os.path.join(dir_path, libname ))

    handle.load_model.argtypes = [load_model_inputs] 
    handle.load_model.restype = ctypes.c_bool
    handle.generate.argtypes = [generation_inputs, ctypes.c_wchar_p] #apparently needed for osx to work. i duno why they need to interpret it that way but whatever
    handle.generate.restype = generation_outputs
    
def load_model(model_filename,batch_size=8,max_context_length=512,n_parts_overwrite=-1,threads=6):
    inputs = load_model_inputs()
    inputs.model_filename = model_filename.encode("UTF-8")
    inputs.batch_size = batch_size
    inputs.max_context_length = max_context_length #initial value to use for ctx, can be overwritten
    inputs.threads = threads
    inputs.n_parts_overwrite = n_parts_overwrite
    inputs.f16_kv = True
    ret = handle.load_model(inputs)
    return ret

def generate(prompt,max_length=20, max_context_length=512,temperature=0.8,top_k=100,top_p=0.85,rep_pen=1.1,rep_pen_range=128,seed=-1):
    inputs = generation_inputs()
    outputs = ctypes.create_unicode_buffer(ctypes.sizeof(generation_outputs))
    inputs.prompt = prompt.encode("UTF-8")
    inputs.max_context_length = max_context_length   # this will resize the context buffer if changed
    inputs.max_length = max_length
    inputs.temperature = temperature
    inputs.top_k = top_k
    inputs.top_p = top_p
    inputs.rep_pen = rep_pen
    inputs.rep_pen_range = rep_pen_range
    inputs.seed = seed
    ret = handle.generate(inputs,outputs)
    if(ret.status==1):
        return ret.text.decode("UTF-8","ignore")
    return ""

#################################################################
### A hacky simple HTTP server simulating a kobold api by Concedo
### we are intentionally NOT using flask, because we want MINIMAL dependencies
#################################################################
friendlymodelname = "concedo/koboldcpp"  # local kobold api apparently needs a hardcoded known HF model name
maxctx = 2048
maxlen = 128
modelbusy = False

class ServerRequestHandler(http.server.SimpleHTTPRequestHandler):
    sys_version = ""
    server_version = "ConcedoLlamaForKoboldServer"

    def __init__(self, addr, port, embedded_kailite):
        self.addr = addr
        self.port = port
        self.embedded_kailite = embedded_kailite

    def __call__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def do_GET(self):
        global maxctx, maxlen, friendlymodelname
        if self.path in ["/", "/?"] or self.path.startswith(('/?','?')): #it's possible for the root url to have ?params without /
            response_body = ""
            if self.embedded_kailite is None:
                response_body = (f"Embedded Kobold Lite is not found.<br>You will have to connect via the main KoboldAI client, or <a href='https://lite.koboldai.net?local=1&port={self.port}'>use this URL</a> to connect.").encode()
            else:
                response_body = self.embedded_kailite

            self.send_response(200)
            self.send_header('Content-Length', str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)
            return
                       
        self.path = self.path.rstrip('/')
        if self.path.endswith(('/api/v1/model', '/api/latest/model')):
            self.send_response(200)
            self.end_headers()
            result = {'result': friendlymodelname }            
            self.wfile.write(json.dumps(result).encode())           
            return

        if self.path.endswith(('/api/v1/config/max_length', '/api/latest/config/max_length')):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"value": maxlen}).encode())
            return

        if self.path.endswith(('/api/v1/config/max_context_length', '/api/latest/config/max_context_length')):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"value": maxctx}).encode())
            return

        if self.path.endswith(('/api/v1/config/soft_prompt', '/api/latest/config/soft_prompt')):
            self.send_response(200)
            self.end_headers()           
            self.wfile.write(json.dumps({"value":""}).encode())
            return
        
        self.send_response(404)
        self.end_headers()
        rp = 'Error: HTTP Server is running, but this endpoint does not exist. Please check the URL.'
        self.wfile.write(rp.encode())
        return
    
    def do_POST(self):
        global modelbusy
        content_length = int(self.headers['Content-Length'])
        body = self.rfile.read(content_length)  
        basic_api_flag = False
        kai_api_flag = False
        self.path = self.path.rstrip('/')

        if modelbusy:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(json.dumps({"detail": {
                    "msg": "Server is busy; please try again later.",
                    "type": "service_unavailable",
                }}).encode())
            return

        if self.path.endswith('/request'):
            basic_api_flag = True

        if self.path.endswith(('/api/v1/generate', '/api/latest/generate')):
            kai_api_flag = True

        if basic_api_flag or kai_api_flag:
            genparams = None
            try:
                genparams = json.loads(body)
            except ValueError as e:
                self.send_response(503)
                self.end_headers()
                return       
            print("\nInput: " + json.dumps(genparams))
            
            modelbusy = True
            if kai_api_flag:
                fullprompt = genparams.get('prompt', "")
            else:
                fullprompt = genparams.get('text', "")
            newprompt = fullprompt
            
            recvtxt = ""
            res = {}
            if kai_api_flag:
                recvtxt = generate(
                    prompt=newprompt,
                    max_context_length=genparams.get('max_context_length', maxctx),
                    max_length=genparams.get('max_length', 50),
                    temperature=genparams.get('temperature', 0.8),
                    top_k=genparams.get('top_k', 200),
                    top_p=genparams.get('top_p', 0.85),
                    rep_pen=genparams.get('rep_pen', 1.1),
                    rep_pen_range=genparams.get('rep_pen_range', 128),
                    seed=-1
                    )
                print("\nOutput: " + recvtxt)
                res = {"results": [{"text": recvtxt}]}                            
            else:
                recvtxt = generate(
                    prompt=newprompt,
                    max_length=genparams.get('max', 50),
                    temperature=genparams.get('temperature', 0.8),
                    top_k=genparams.get('top_k', 200),
                    top_p=genparams.get('top_p', 0.85),
                    rep_pen=genparams.get('rep_pen', 1.1),
                    rep_pen_range=genparams.get('rep_pen_range', 128),
                    seed=-1
                    )
                print("\nOutput: " + recvtxt)
                res = {"data": {"seqs":[recvtxt]}}

            try:  
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(res).encode())
            except:
                print("Generate: The response could not be sent, maybe connection was terminated?")
            modelbusy = False
            return    
        self.send_response(404)
        self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()
    
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', '*')
        self.send_header('Access-Control-Allow-Headers', '*')
        if "/api" in self.path:
            self.send_header('Content-type', 'application/json')
        else:
            self.send_header('Content-type', 'text/html')
           
        return super(ServerRequestHandler, self).end_headers()


def RunServerMultiThreaded(addr, port, embedded_kailite = None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((addr, port))
    sock.listen(5)

    class Thread(threading.Thread):
        def __init__(self, i):
            threading.Thread.__init__(self)
            self.i = i
            self.daemon = True
            self.start()

        def run(self):
            handler = ServerRequestHandler(addr, port, embedded_kailite)
            with http.server.HTTPServer((addr, port), handler, False) as self.httpd:
                try:
                    self.httpd.socket = sock
                    self.httpd.server_bind = self.server_close = lambda self: None
                    self.httpd.serve_forever()
                except (KeyboardInterrupt,SystemExit):
                    self.httpd.server_close()
                    sys.exit(0)
                finally:
                    self.httpd.server_close()
                    sys.exit(0)
        def stop(self):
            self.httpd.server_close()

    numThreads = 6
    threadArr = []
    for i in range(numThreads):
        threadArr.append(Thread(i))
    while 1:
        try:
            time.sleep(10)
        except KeyboardInterrupt:
            for i in range(numThreads):
                threadArr[i].stop()
            sys.exit(0)

def main(args): 
    global use_blas
    if not os.path.exists(os.path.join(os.path.dirname(os.path.realpath(__file__)), "libopenblas.dll")) or not os.path.exists(os.path.join(os.path.dirname(os.path.realpath(__file__)), "koboldcpp_blas.dll")):
        print("Warning: libopenblas.dll or koboldcpp_blas.dll not found. Non-BLAS library will be used. Ignore this if you have manually linked with OpenBLAS.")
        use_blas = False
    elif os.name != 'nt':
        print("Prebuilt OpenBLAS binaries only available for windows. Please manually build/link libopenblas from makefile with LLAMA_OPENBLAS=1")
        use_blas = False
    elif not args.noblas:
        print("Attempting to use OpenBLAS library for faster prompt ingestion. A compatible libopenblas.dll will be required.")
        use_blas = True
    init_library() # Note: if blas does not exist and is enabled, program will crash.
    ggml_selected_file = args.model_file
    embedded_kailite = None 
    if not ggml_selected_file:     
        #give them a chance to pick a file
        print("Please manually select ggml file:")
        from tkinter.filedialog import askopenfilename
        ggml_selected_file = askopenfilename (title="Select ggml model .bin files")
        if not ggml_selected_file:
            print("\nNo ggml model file was selected. Exiting.")
            time.sleep(1)
            sys.exit(2)

    if not os.path.exists(ggml_selected_file):
        print(f"Cannot find model file: {ggml_selected_file}")
        time.sleep(1)
        sys.exit(2)

    mdl_nparts = sum(1 for n in range(1, 9) if os.path.exists(f"{ggml_selected_file}.{n}")) + 1
    modelname = os.path.abspath(ggml_selected_file)
    print(f"Loading model: {modelname} \n[Parts: {mdl_nparts}, Threads: {args.threads}]")
    loadok = load_model(modelname,8,maxctx,mdl_nparts,args.threads)
    print("Load Model OK: " + str(loadok))

    if not loadok:
        print("Could not load model: " + modelname)
        time.sleep(1)
        sys.exit(3)
    try:
        basepath = os.path.abspath(os.path.dirname(__file__))
        with open(os.path.join(basepath, "klite.embd"), mode='rb') as f:
            embedded_kailite = f.read()
            print("Embedded Kobold Lite loaded.")
    except:
        print("Could not find Kobold Lite. Embedded Kobold Lite will not be available.")

    print(f"Starting Kobold HTTP Server on port {args.port}")
    epurl = ""
    if args.host=="":
        epurl = f"http://localhost:{args.port}" + ("?streaming=1" if args.stream else "")   
    else:
        epurl = f"http://{args.host}:{args.port}?host={args.host}" + ("&streaming=1" if args.stream else "")   
    
        
    print(f"Please connect to custom endpoint at {epurl}")
    RunServerMultiThreaded(args.host, args.port, embedded_kailite)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Kobold llama.cpp server')
    parser.add_argument("model_file", help="Model file to load", nargs="?")
    portgroup = parser.add_mutually_exclusive_group() #we want to be backwards compatible with the unnamed positional args
    portgroup.add_argument("--port", help="Port to listen on", default=5001, type=int)
    portgroup.add_argument("port", help="Port to listen on", default=5001, nargs="?", type=int)
    parser.add_argument("--host", help="Host IP to listen on. If empty, all routable interfaces are accepted.", default="")
    
    # psutil.cpu_count(logical=False)
    physical_core_limit = 1 
    if os.cpu_count()!=None and os.cpu_count()>1:
        physical_core_limit = int(os.cpu_count()/2)
    default_threads = (physical_core_limit if physical_core_limit<=3 else max(3,physical_core_limit-1))
    parser.add_argument("--threads", help="Use a custom number of threads if specified. Otherwise, uses an amount based on CPU cores", type=int, default=default_threads)
    parser.add_argument("--stream", help="Uses pseudo streaming", action='store_true')
    parser.add_argument("--noblas", help="Do not use OpenBLAS for accelerated prompt ingestion", action='store_true')
    args = parser.parse_args()
    main(args)
