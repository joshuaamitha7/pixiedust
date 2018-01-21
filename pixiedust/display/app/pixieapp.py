# -------------------------------------------------------------------------------
# Copyright IBM Corp. 2017
# 
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
# http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# -------------------------------------------------------------------------------
from pixiedust.display import display
from pixiedust.display.display import *
from pixiedust.utils.shellAccess import ShellAccess
from pixiedust.utils import Logger
from six import iteritems, string_types
from collections import OrderedDict, namedtuple
import base64
import inspect
import sys
from functools import partial
from six import string_types
from IPython.utils.io import capture_output

def route(**kw):
    def route_dec(fn):
        fn.pixiedust_route = kw
        return fn
    return route_dec

@Logger()
class captureOutput(object):
    """
    Decorator used for routes that allows using external libraries for generating
    the html fragment. 
    When using this decorator the route doesn't need to return a string. If it does
    it will be ignored.
    Must be declared in after the route decorator. 
    captureOutput and templateArgs should not be used together
        from pixiedust.display.app import *
        import matplotlib.pyplot as plt
        import numpy as np
        @PixieApp
        class Test():
            @route()
            @captureOutput
            def mainScreen(self):
                t = np.arange(0.0, 2.0, 0.01)
                s = 1 + np.sin(2*np.pi*t)
                plt.plot(t, s)

                plt.xlabel('time (s)')
                plt.ylabel('voltage (mV)')
                plt.title('About as simple as it gets, folks')
                plt.grid(True)
                plt.savefig("test.png")
                plt.show()
        Test().run()
    
    """
    def __init__(self, fn):
        self.fn = fn

    def convert_html(self, output):
        if "text/html" in output.data:
            return output._repr_html_()
        elif "image/png" in output.data:
            return """<img alt="image" src="data:image/png;base64,{}"><img>""".format(
                base64.b64encode(output._repr_png_()).decode("ascii")
            )
        elif "application/javascript" in output.data:
            return """<script type="text/javascript">{}</script>""".format(output._repr_javascript_())
        elif "text/markdown" in output.data:
            import markdown
            return markdown.markdown(output._repr_mime_("text/markdown"))
        self.debug("Unused output: {}".format(output.data.keys()))
        return ""

    def __get__(self, instance, instance_type):
        wrapper_fn = partial(self.wrapper, instance)
        wrapper_fn.org_fn = self.fn
        return wrapper_fn

    def wrapper(self, instance, *args, **kwargs):
        with capture_output() as buf:
            self.fn(instance, *args, **kwargs)
        return "\n".join([self.convert_html(output) for output in buf.outputs])

class templateArgs(object):
    """
    Decorator that enables using local variable in a Jinja template.
    Must be used in conjunction with route decorator and declared after
        from pixiedust.display.app import *
        @PixieApp
        class Test():
            @route()
            @templateArgs
            def mainScreen(self):
                var1 = 'something computed'
                return "<div>Accessing local variable {{var1}} from a jinja template"
        Test().run()
    """
    TemplateRetValue = namedtuple('TemplateRetValue', ['ret_value', 'locals'])
    def __init__(self, fn):
        self.fn = fn

    def __get__(self, instance, instance_type):
        wrapper_fn = partial(self.wrapper, instance)
        wrapper_fn.org_fn = self.fn
        return wrapper_fn

    def wrapper(self, instance, *args, **kwargs):
        locals = [{}]
        def tracer(frame, event, arg):
            if event == "return":
                locals[0] = frame.f_locals.copy()
                if 'self' in locals[0]:
                    del locals[0]['self']
        sys.setprofile(tracer)
        try:
            ret_value = self.fn(instance, *args, **kwargs)
            return templateArgs.TemplateRetValue(ret_value, locals[0])
        finally:
            sys.setprofile(None)

#Global object enables system wide customization of PixieApp run option
pixieAppRunCustomizer = None

def runPixieApp(app, parentApp=None, entity=None, **kwargs):
    if isinstance(app, PixieDustApp):
        app.run(entity, **kwargs)
    elif isinstance(app, string_types):
        parts = app.split('.')
        getattr(__import__('.'.join(parts[:-1]), None, None, [parts[-1]], 0), parts[-1])().run(entity, **kwargs)
    else:
        raise ValueError("Invalid argument to runPixieApp. Only PixieApp or String allowed")

@Logger()
class PixieDustApp(Display):

    routesByClass = {}

    def getOptionValue(self, optionName):
        #first check if the key is an field of the class
        option = getattr(self.entity, optionName) if self.entity is not None and hasattr(self.entity, optionName) else None
        #make sure we don't have a conflict with an existing function
        if callable(option):
            option = None
        if option is None:
            option = self.options.get(optionName, None)
        return option

    def matchRoute(self, route):
        for key,value in iteritems(route):
            option = self.getOptionValue(key)
            if  (option is None and value=="*") or (value != "*" and option != value):
                return False
        return True

    def injectArgs(self, method, route):
        if isinstance(method, partial) and hasattr(method, "org_fn"):
            method = method.org_fn
        argspec = inspect.getargspec(method)
        args = argspec.args
        if len(args) > 0:
            args = args[1:] if hasattr(method, "__self__") or args[0] == 'self' else args
        return OrderedDict(zip([a for a in args], [self.getOptionValue(arg) for arg in args]))

    def invoke_route(self, class_method, **kwargs):
        "Programmatically invoke a route from arguments"
        try:
            injectedArgs = kwargs
            retValue = class_method(*list(injectedArgs.values()))
        finally:
            if isinstance(retValue, templateArgs.TemplateRetValue):
                injectedArgs.update(retValue.locals)
                retValue = retValue.ret_value
            if isinstance(retValue, string_types):
                retValue = self.renderTemplateString(retValue, **injectedArgs)
        return retValue

    def __getattr__(self, name):
        if ShellAccess[name] is not None:
            return ShellAccess[name]
        if name != "__pd_gateway_namespace__" and hasattr(self, "__pd_gateway_namespace__"):
            name = self.__pd_gateway_namespace__ + name
            if ShellAccess[name] is not None:
                return ShellAccess[name]
        raise AttributeError("{} attribute not found".format(name))

    def doRender(self, handlerId):
        if self.__class__.__name__ in PixieDustApp.routesByClass:
            defRoute = None
            retValue = None
            injectedArgs = {}
            try:
                dispatchKey = "widgets" if "widget" in self.options else "routes"
                for t in PixieDustApp.routesByClass[self.__class__.__name__][dispatchKey]:
                    if not t[0]:
                        defRoute = t[1]
                    elif self.matchRoute(t[0]):
                        self.debug("match found: {}".format(t[0]))
                        meth = getattr(self, t[1])
                        injectedArgs = self.injectArgs(meth, t[0])
                        self.debug("Injected args: {}".format(injectedArgs))
                        retValue = meth(*list(injectedArgs.values()))
                        return
                if defRoute:
                    retValue = getattr(self, defRoute)()
                    return
            finally:
                if isinstance(retValue, templateArgs.TemplateRetValue):
                    injectedArgs.update(retValue.locals)
                    retValue = retValue.ret_value
                if isinstance(retValue, string_types):
                    self._addHTMLTemplateString(retValue, **injectedArgs)
                elif isinstance(retValue, dict):
                    body = self.renderTemplateString(retValue.get("body", ""))
                    jsOnLoad = self.renderTemplateString(retValue.get("jsOnLoad", ""))
                    jsOK = self.renderTemplateString(retValue.get("jsOK", ""))
                    dialogRoot = retValue.get("dialogRoot", None)
                    if dialogRoot is not None:
                        jsOnLoad = """pixiedust.dialogRoot="{}";\n{}""".format(self.renderTemplateString(dialogRoot), jsOnLoad)
                    if body is not None:
                        self._addHTMLTemplateString("""
                        {{body}}
                        <pd_dialog>
                            <pd_onload>{{jsOnLoad|htmlAttribute}}</pd_onload>
                            <pd_ok>{{jsOK|htmlAttribute}}</pd_ok>
                        </pd_dialog>
                        """, body=body, jsOnLoad=jsOnLoad, jsOK=jsOK)

        print("Didn't find any routes for {}".format(self))

    def get_custom_options(self):
        return {}

    def getDialogOptions(self):
        return {}

@Logger()
def PixieApp(cls):
    #reset the class routing in case the cell is being run multiple time
    clsName = "{}_{}_Display".format(inspect.getmodule(cls).__name__, cls.__name__)
    PixieDustApp.routesByClass[clsName] = {"routes":[], "widgets":[]}
    #put the routes that define a widget in a separate bucket

    def walk(cl):
        for name, method in iteritems(cl.__dict__):
            if hasattr(method, "pixiedust_route"):
                if "widget" in method.pixiedust_route:
                    PixieDustApp.routesByClass[clsName]["widgets"].append( (method.pixiedust_route,name) )
                else:
                    PixieDustApp.routesByClass[clsName]["routes"].append( (method.pixiedust_route,name) )
        for c in [c for c in cl.__bases__]:
            walk(c)
    walk(cls)

    #re-order the routes according to the number of constraints e.g. from more to less specific
    p = PixieDustApp.routesByClass[clsName]["routes"]
    PixieDustApp.routesByClass[clsName]["routes"] = [p[a[1]] for a in sorted([(len(a[0]), i) for i,a in enumerate(p)], reverse=True)]

    def __init__(self, options=None, entity=None, dataHandler=None):
        PixieDustApp.__init__(self, options or {}, entity, dataHandler)

    def getPixieAppEntity(self):
        return self.pixieapp_entity if hasattr(self, "pixieapp_entity") else None

    def formatOptions(self,options):
        """Helper method that convert pd options from Json format to pixieApp html attribute compliant format"""
        return ';'.join(["{}={}".format(key,value) for (key, value) in iteritems(options)])

    def decoName(cls, suffix):
        return "{}_{}_{}".format(cls.__module__, cls.__name__, suffix)

    def run(self, entity=None, **kwargs):
        for key,value in iteritems(kwargs):
            setattr(self, key, value)
        if entity is not None:
            self.pixieapp_entity = entity
        var = None
        for key in ShellAccess:
            if ShellAccess[key] is self:
                var = key

        if not hasattr(self, "pd_initialized"):
            if hasattr(self, "setup"):
                self.setup()
            self.nostore_params = True
            self.pd_initialized = True

        if not var:
            #If we're here, the user must have created the instance inline, assign a variable dynamically
            var = cls.__name__ + "_instance"
            ShellAccess[var] = self

        self.runInDialog = kwargs.get("runInDialog", "false") is "true"
        options = {"nostore_pixieapp": var, "nostore_ispix":"true", "runInDialog": "true" if self.runInDialog else "false"}
        #update with any custom options that the pixieapp may have
        options.update(self.get_custom_options())

        if self.runInDialog:
            options.update(self.getDialogOptions())

        options.update({'handlerId': decoName(cls, "id")})
        if "options" in kwargs and isinstance(kwargs['options'], dict):
            options.update(kwargs['options'])
        if pixieAppRunCustomizer is not None and callable(getattr(pixieAppRunCustomizer, "customizeOptions", None)):
            pixieAppRunCustomizer.customizeOptions(options)

        opts = [(k, str(v).lower() if isinstance(v, bool) else v) for (k,v) in iteritems(options) if v is not None]
        s = "display({}{})".format(var, reduce(lambda k,v: k + "," + v[0] + "='" + str(v[1]) + "'", opts, ""))
        try:
            sys.modules['pixiedust.display'].pixiedust_display_callerText = s
            locals()[var] = self
            return eval(s, globals(), locals())
        finally:
            del sys.modules['pixiedust.display'].pixiedust_display_callerText
        
    displayClass = type( decoName(cls, "Display"), (cls,PixieDustApp, ),{
        "__init__": __init__, 
        "run": run, 
        "getPixieAppEntity":getPixieAppEntity
    })
    ShellAccess["newDisplayClass"] = displayClass

    def prettyFormat(o):
        return "{} at {}".format(o, id(o))
    
    @addId
    def getMenuInfo(self, entity, dataHandler=None):
        if entity is displayClass or entity.__class__ is displayClass:
            return [{"id": decoName(cls, "id")}]
        return []

    def newDisplayHandler(self, options, entity):
        if entity is displayClass or entity.__class__ is displayClass:
            entity.__init__(options, entity)
            return entity
        elif options.get("nostore_pixieapp") is not None:
            from pixiedust.utils.shellAccess import ShellAccess
            papp = ShellAccess[options.get("nostore_pixieapp")]
            if papp is not None and hasattr(papp, "newDisplayHandler"):
                fn = papp.newDisplayHandler
                if callable(fn):
                    return fn(options, entity)
        return None

    displayHandlerMetaClass = type( decoName(cls, "Meta"), (DisplayHandlerMeta,), {
            "getMenuInfo": getMenuInfo,
            "newDisplayHandler": newDisplayHandler
        })

    displayHandlerMeta = displayHandlerMetaClass()
    ShellAccess["displayHandlerMeta"] = displayHandlerMeta
    registerDisplayHandler( displayHandlerMeta )
    return displayClass
