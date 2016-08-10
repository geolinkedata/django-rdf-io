# # -*- coding:utf-8 -*-
from django.shortcuts import render_to_response, redirect
from .models import ObjectMapping,Namespace,AttributeMapping,EmbeddedMapping, ObjectType, getattr_path, apply_pathfilter, expand_curie
from django.template import RequestContext
from django.contrib.contenttypes.models import ContentType
from django.conf import settings
from string import Formatter
from rdflib import BNode

import requests

from django.shortcuts import get_object_or_404
# deprecated since 1.3
# from django.views.generic.list_detail import object_list
# but not used anyway?
# if needed.. from django.views.generic import ListView

from django.http import HttpResponse,Http404

from rdflib import Graph,namespace
from rdflib.term import URIRef, Literal
from rdflib.namespace import NamespaceManager,RDF


import logging
logger = logging.getLogger(__name__)

_nslist = {}

def _getNamespace( prefix ) :
    if not _nslist.has_key( prefix ) :
         ns = Namespace.objects.get(prefix = prefix)
         if ns: 
            _nslist[ prefix ] = ns.uri
         else :
            _nslist[ prefix ] = None
    return _nslist[prefix]
    
def _as_resource(gr,curie) :
    cleaned = str(curie).translate(None,'"\'<>')
    if cleaned[0:4] == 'http' :
        return URIRef(cleaned)
    # this will raise error if not valid curie format
    (ns,value) = cleaned.split(":",2)
    
    try :
        return URIRef("".join((_getNamespace(ns),value)))
    except:
        raise ValueError("prefix " + ns + "not recognised")
 
 
def to_rdfbykey(request,model,key):
    """
        take a model name + object id reference to an instance and apply any RDF serialisers defined for this
        allows a key to de appended to the uri or supplied by parameter (easier for uri values)
    """
    if request.GET.get('key'):
        key = request.GET.get('key')
    try: 
        return _tordf(request,model,None,key)
    except Exception as e: 
        return HttpResponse("Model not serialisable to RDF: %s" % e, status=500)
 
def to_rdfbyid(request,model,id):
    """
        take a model name + object id reference to an instance and apply any RDF serialisers defined for this
    """
    try: 
        return _tordf(request,model,id,None)
    except Exception as e: 
        return HttpResponse("Model not serialisable to RDF: %s" % e, status=500)

def _tordf(request,model,id,key):
    if request.GET.get('pdb') :
        import pdb; pdb.set_trace()
    format = 'json-ld'
    if request.GET.get('_format') :
        format = request.GET.get('_format')
    # find the model type referenced
    ct = ContentType.objects.get(model=model)
    if not ct :
        raise Http404("No such model found")
    oml = ObjectMapping.objects.filter(content_type=ct)
    if not oml :
        return HttpResponse("Model not serialisable to RDF", status=410 )
    if id :    
        obj = get_object_or_404(ct.model_class(), pk=id)
    else :
        obj = ct.model_class().objects.get_by_natural_key(key)
    
    # ok so object exists and is mappable, better get down to it..
 
    includemembers = False
    
    gr = Graph()
#    import pdb; pdb.set_trace()
#    ns_mgr = NamespaceManager(Graph())
#    gr.namespace_manager = ns_mgr
    try:
        gr = build_rdf(gr, obj, oml, includemembers)
    except Exception as e:
        raise Http404("Error during serialisation: " + str(e) )
    for ns in _nslist.keys() :
        gr.namespace_manager.bind( str(ns), namespace.Namespace(str(_nslist[ns])), override=False)
    return HttpResponse(content_type="text/turtle", content=gr.serialize(format=format))

def pub_rdf(request,model,id):
    """
        take a model name + object id reference to an instance serialise and push to the configured triplestore
    """
    if request.GET.get('pdb') :
        import pdb; pdb.set_trace()
    # find the model type referenced
    ct = ContentType.objects.get(model=model)
    if not ct :
        raise Http404("No such model found")
    oml = ObjectMapping.objects.filter(content_type=ct)
    if not oml :
        raise HttpResponse("Model not serialisable to RDF", status=410 )
    
    obj = get_object_or_404(ct.model_class(), pk=id)
    # ok so object exists and is mappable, better get down to it..
   
    result = publish(obj, model, oml)
    return HttpResponse(result.content,status=result.status_code )
    
def publish(obj, model, oml ):
    # now get the remote store mappings 
    try:
        rdfstore = settings.RDFSTORE['default']
        auth = rdfstore.get('auth')
        server = rdfstore['server']
    except:
        return  HttpResponse("RDF store not configured", status=410 )
        
    try:
        rdfstore = settings.RDFSTORE[model]
        if not rdfstore.has_key('server') :
            rdfstore['server'] = server
            rdfstore['auth'] = auth
    except:
        pass  # use default then
 
    gr = Graph()
#    import pdb; pdb.set_trace()
#    ns_mgr = NamespaceManager(Graph())
#    gr.namespace_manager = ns_mgr
    try:
        gr = build_rdf(gr, obj, oml, False)
    except Exception as e:
        return  HttpResponse("Error during serialisation: " + str(e) , status=500 )
    for ns in _nslist.keys() :
        gr.namespace_manager.bind( str(ns), namespace.Namespace(str(_nslist[ns])), override=False)
    
#    curl -X POST -H "Content-Type: text/turtle" -d @- http://192.168.56.151:8080/marmotta/import/upload?context=http://mapstory.org/def/featuretypes/gazetteer 
    resttgt = "".join( ( rdfstore['server'],_resolveTemplate(rdfstore['target'], model, obj ) ))  

    etag = _get_etag(resttgt)
    headers = {'Content-Type': 'text/turtle'} 
    if etag :
        headers['If-Match'] = etag
       
    for h in rdfstore.get('headers') or [] :
        headers[h] = _resolveTemplate( rdfstore['headers'][h], model, obj )
    
    
    result = requests.put( resttgt, headers=headers , data=gr.serialize(format="turtle"), auth=rdfstore.get('auth'))
    logger.info ( "Updating resource {} {}".format(resttgt,result.status_code) )
    if result.status_code > 400 :
#         print "Posting new resource"
#         result = requests.post( resttgt, headers=headers , data=gr.serialize(format="turtle"))
        logger.error ( "Failed to publish resource {} {}".format(resttgt,result.status_code) )
        return HttpResponse ("Failed to publish resource {} {}".format(resttgt,result.status_code) , status = result.status_code )
    return result 

def _get_etag(uri):
    """
        Gets the LDP Etag for a resource if it exists
    """
    # could put in cache here - but for now just issue a HEAD
    result = requests.head(uri)
    return result.headers.get('ETag')
    
def _resolveTemplate(template, model, obj) :
    
    vals = { 'model' : model }
    for (literal,param,repval,conv) in Formatter().parse(template) :
        if param and param != 'model' :
            try:
                vals[param] = getattr_path(obj,param).pop()
            except:
                if param == 'slug'  :
                    vals[param] = obj.id
    
    return template.format(**vals)
 
   
def build_rdf( gr,obj, oml, includemembers ) :  

    # would be nice to add some comments : as metadata on the graph? '# Turtle generated by django-rdf-io configurable serializer\n'  
    for om in oml :
        # check filter
        objfilter = getattr(om,'filter') 
        if objfilter and not apply_pathfilter(obj, objfilter ) :
            continue
        try:
            tgt_id = getattr_path(obj,om.id_attr)[0]
        except ValueError as e:
            raise ValueError("target id attribute {} not found".format( (om.id_attr ,)))
        if om.target_uri_expr[0] == '"' :   
            uribase = om.target_uri_expr[1:-1]
        else:
            uribase = getattr_path(obj,om.target_uri_expr)[0]
            
        tgt_id = str(tgt_id).replace(uribase,"")
        # strip uri base if present in tgt_id
        uribase = expand_curie(uribase)
        
 
        if not tgt_id:
            uri = uribase
        elif uribase[-1] == '/' or uribase[-1] == '#' :
            uri = "".join((uribase,tgt_id))
        else :
            uri = "/".join((uribase,tgt_id))
        
        subject = URIRef(uri)
        
        for omt in om.obj_type.all() :
            gr.add( (subject, RDF.type , _as_resource(gr,omt.uri)) )
  
        # now get all the attribute mappings and add these in
        for am in AttributeMapping.objects.filter(scope=om) :
            _add_vals(gr, obj, subject, am.predicate, am.attr , am.is_resource)
        for em in EmbeddedMapping.objects.filter(scope=om) :
            try:
                # three options - scalar value in which case attributes relative to basic obj, a mulitvalue obj or we have to look for related objects
                valuelist = [obj,]
                try:
                    attrvalue = getattr(obj,em.attr)
                    valuelist = attrvalue.all()
                except:
                    # last thing to try - look for related models...
                    try:
                        valuelist = getattr(obj, "_".join((em.attr.lower(),'set'))).all()
                    except:
                        pass # revert to scalar 
                    
                for value in valuelist :
                    newnode = BNode()
                    gr.add( (subject, _as_resource(gr,em.predicate) , newnode) )
                    for element in em.struct.split(";") :
                        (predicate,expr) = element.split()
                        if expr.startswith("<") :
                            is_resource = True
                            expr = expr[1:-1]
                        else:
                            is_resource = False
                        _add_vals(gr, value, newnode, predicate, expr , is_resource)
            except:
                raise ValueError("Could not evaluate express em.struct ")
        return gr

def _add_vals(gr, obj, subject, predicate, attr, is_resource ) :       
            if attr[0] in '\'\"' : # the a literal
                if is_resource :
                    gr.add( (subject, _as_resource(gr,predicate) , _as_resource(gr,attr) ) )
                else:
                    gr.add( (subject, _as_resource(gr,predicate) , Literal(attr) ))
            else :
                values = getattr_path(obj,attr)
                for value in values :
                    if not value :
                        continue
                    if is_resource :
                        object = _as_resource(gr,value)
                    else:
                        try :
                            (value,valtype) = value.split("^^")
                            object = Literal(value,datatype=valtype)
                        except:
                            try :
                                (value,valtype) = value.split("@")
                                object = Literal(value,lang=valtype)
                            except:
                                object = Literal(value)
                            
                    gr.add( (subject, _as_resource(gr,predicate) , object) )
    
def sync_remote(request,models):
    """
        Synchronises the RDF published output for the models, in the order listed (list containers before members!)
    """
    if request.GET.get('pdb') :
        import pdb; pdb.set_trace()
 
    for modelname in models.split(",") :
         do_sync_remote( modelname )
    return HttpResponse("sync successful for {}".format(models), status=200)
    
def do_sync_remote(formodel):
    ct = ContentType.objects.get(model=formodel)
    oml = ObjectMapping.objects.filter(content_type=ct)
    modelclass = ct.model_class()
    for obj in modelclass.objects.all() :
        publish( obj, formodel, oml)
# gr.add((URIRef('skos:Concept'), RDF.type, URIRef('foaf:Person')))
# gr.add((URIRef('rdf:Concept'), RDF.type, URIRef('xxx:Person')))