/// IVG Cypher Engine - submit Cypher queries from ObjectScript.
/// Works against the local IRIS instance or any remote IRIS.
///
/// Local (same IRIS instance):
///   Set engine = ##class(IVG.CypherEngine).Local()
///   Set result = engine.Query("MATCH (n:Person) RETURN n.name LIMIT 5")
///
/// Remote (another IRIS instance):
///   Set engine = ##class(IVG.CypherEngine).Remote("other-host", 1972, "USER", "_SYSTEM", "SYS")
///   Set result = engine.Query("MATCH (n) RETURN count(n) AS cnt")
///
/// result is a %DynamicObject: {columns: %DynamicArray, rows: %DynamicArray, error: ""}
///
Class IVG.CypherEngine Extends %RegisteredObject
{

Property ConnMode As %String  [ Internal, Private ];
Property ConnHost As %String  [ Internal, Private ];
Property ConnPort As %Integer [ Internal, Private ];
Property ConnNS   As %String  [ Internal, Private ];
Property ConnUser As %String  [ Internal, Private ];
Property ConnPass As %String  [ Internal, Private ];
Property EmbedDim As %Integer [ InitialExpression = 768, Internal, Private ];
Property Inited   As %Boolean [ InitialExpression = 0, Internal, Private ];
Property PyEngine As %String  [ Internal, Private ];

ClassMethod Local(embeddingDimension As %Integer = 768) As IVG.CypherEngine
{
    Set obj = ..%New()
    Set obj.ConnMode = "embedded"
    Set obj.EmbedDim = embeddingDimension
    Quit obj
}

ClassMethod Remote(
    host      As %String,
    port      As %Integer = 1972,
    namespace As %String  = "USER",
    username  As %String  = "_SYSTEM",
    password  As %String  = "SYS",
    embeddingDimension As %Integer = 768
) As IVG.CypherEngine
{
    Set obj = ..%New()
    Set obj.ConnMode = "remote"
    Set obj.ConnHost = host
    Set obj.ConnPort = port
    Set obj.ConnNS   = namespace
    Set obj.ConnUser = username
    Set obj.ConnPass = password
    Set obj.EmbedDim = embeddingDimension
    Quit obj
}

/// Initialize Python engine — called lazily by all methods.
Method EnsureEngine() [ Language = python ]
{
    if self.Inited:
        return
    from iris_vector_graph.engine import IRISGraphEngine
    if self.ConnMode == 'remote':
        import sys
        for p in ['/usr/irissys/mgr/python', '/usr/irissys/lib/python']:
            if p in sys.path:
                sys.path.remove(p)
            sys.path.insert(0, p)
        for mod in list(sys.modules.keys()):
            if mod == 'iris' or mod.startswith('iris.'):
                del sys.modules[mod]
        import iris
        conn = iris.connect(
            hostname=str(self.ConnHost),
            port=int(self.ConnPort),
            namespace=str(self.ConnNS),
            username=str(self.ConnUser),
            password=str(self.ConnPass),
        )
        self.PyEngine = IRISGraphEngine(conn, embedding_dimension=int(self.EmbedDim))
    else:
        import sys as _sys, importlib as _il
        for _p in ['/usr/irissys/mgr/python', '/usr/irissys/lib/python']:
            if _p in _sys.path:
                _sys.path.remove(_p)
            _sys.path.insert(0, _p)
        for _m in list(_sys.modules.keys()):
            if _m == 'iris' or _m.startswith('iris.'):
                del _sys.modules[_m]
        _iris_embedded = _il.import_module('iris')
        from iris_vector_graph.embedded import EmbeddedConnection
        self.PyEngine = IRISGraphEngine(
            EmbeddedConnection(_iris_embedded.sql), embedding_dimension=int(self.EmbedDim)
        )
    self.Inited = True
}

/// Execute a Cypher query. Returns JSON string: {"columns":[...],"rows":[[...],...],"error":""}
Method QueryJson(cypher As %String, params As %String = "{}") As %String [ Language = python ]
{
    import json
    self.EnsureEngine()
    p = {}
    if params and params not in ('{}', ''):
        try:
            p = json.loads(params)
        except Exception:
            pass
    r = self.PyEngine.execute_cypher(cypher, parameters=p)
    cols = r.get('columns') or []
    rows = r.get('rows') or []
    safe_rows = []
    for row in rows:
        cells = list(row) if hasattr(row, '__iter__') and not isinstance(row, str) else [row]
        safe_row = []
        for cell in cells:
            if cell is None:
                safe_row.append(None)
            elif isinstance(cell, (list, tuple, dict)):
                safe_row.append(str(cell))
            else:
                try:
                    json.dumps(cell)
                    safe_row.append(cell)
                except Exception:
                    safe_row.append(str(cell))
        safe_rows.append(safe_row)
    return json.dumps({"columns": cols, "rows": safe_rows, "error": r.get('error') or ''})
}

/// Execute a Cypher query. Returns %DynamicObject: {columns, rows, error}
Method Query(cypher As %String, params As %String = "{}") As %DynamicObject
{
    Quit ##class(%DynamicObject).%FromJSON(..QueryJson(cypher, params))
}

/// Return first column of first row as string. Useful for COUNT queries.
Method Scalar(cypher As %String, default As %String = "") As %String [ Language = python ]
{
    self.EnsureEngine()
    r = self.PyEngine.execute_cypher(cypher)
    for row in (r.get('rows') or []):
        cells = list(row) if hasattr(row, '__iter__') and not isinstance(row, str) else [row]
        if cells:
            return '' if cells[0] is None else str(cells[0])
    return default
}

/// Return node labels as JSON array string.
Method GetLabelsJson() As %String [ Language = python ]
{
    import json
    self.EnsureEngine()
    return json.dumps(self.PyEngine.get_labels())
}

/// Return node labels as %DynamicArray.
Method GetLabels() As %DynamicArray
{
    Quit ##class(%DynamicArray).%FromJSON(..GetLabelsJson())
}

/// Return node count, optionally filtered by label.
Method GetNodeCount(label As %String = "") As %Integer [ Language = python ]
{
    self.EnsureEngine()
    return self.PyEngine.get_node_count(label if label else None)
}

/// Return edge count, optionally filtered by predicate.
Method GetEdgeCount(predicate As %String = "") As %Integer [ Language = python ]
{
    self.EnsureEngine()
    return self.PyEngine.get_edge_count(predicate if predicate else None)
}

/// Return 1 if node exists, 0 otherwise.
Method NodeExists(nodeId As %String) As %Boolean [ Language = python ]
{
    self.EnsureEngine()
    return 1 if self.PyEngine.node_exists(str(nodeId)) else 0
}

/// Return label distribution as JSON string: {"Label": count, ...}
Method GetLabelCountsJson() As %String [ Language = python ]
{
    import json
    self.EnsureEngine()
    return json.dumps(self.PyEngine.get_label_distribution())
}

/// Return label distribution as %DynamicObject.
Method GetLabelCounts() As %DynamicObject
{
    Quit ##class(%DynamicObject).%FromJSON(..GetLabelCountsJson())
}

/// Initialize schema. Returns %DynamicObject with status fields.
Method InitSchemaJson() As %String [ Language = python ]
{
    import json
    self.EnsureEngine()
    try:
        result = self.PyEngine.initialize_schema()
    except Exception as ex:
        msg = str(ex)
        m = msg.lower()
        # IRISHealth / no Vector Search: #15806, missing KG_NODEEMBEDDINGS, or vector stored-proc install.
        vector_partial = (
            ('15806' in msg)
            or ('vector search' in m and 'not permitted' in m)
            or ('vector' in m and 'not permitted' in m)
            or ('kg_nodeembeddings' in m)
            or ('server-side vector search will be unavailable' in m)
            or ('failed to install' in m and 'stored procedure' in m)
        )
        if vector_partial:
            result = {
                'status': 'partial',
                'vector_skipped': True,
                'warning': msg,
            }
        else:
            raise
    return json.dumps({k: str(v) for k, v in result.items()})
}

Method InitSchema() As %DynamicObject
{
    Quit ##class(%DynamicObject).%FromJSON(..InitSchemaJson())
}

/// Rebuild ^KG adjacency index. Returns 1 on success.
Method RebuildKG() As %Boolean [ Language = python ]
{
    self.EnsureEngine()
    return 1 if self.PyEngine.rebuild_kg() else 0
}

/// Return 1 if engine is ready to serve queries.
Method IsReady() As %Boolean [ Language = python ]
{
    try:
        self.EnsureEngine()
        return 1 if self.PyEngine.is_ready() else 0
    except Exception:
        return 0
}

/// Run pip install for ``iris-vector-graph[full]`` in-process; Return JSON with rc and log tails.
ClassMethod PipInstallIrisVectorGraphJson() As %String [ Language = python ]
{
    import io
    import json
    from contextlib import redirect_stderr, redirect_stdout

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    pip_rc = 0
    try:
        from pip._internal.cli.main import main as pip_main

        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            try:
                pip_main(["install", "iris-vector-graph[full]"])
            except SystemExit as ex:
                code = ex.code
                pip_rc = int(code) if code is not None else 1
    except Exception as ex:
        pip_rc = 1
        buf_err.write(str(ex))
    return json.dumps(
        {
            "pip_rc": int(pip_rc),
            "pip_stdout_tail": buf_out.getvalue()[-1500:],
            "pip_stderr_tail": buf_err.getvalue()[-1500:],
        }
    )
}

/// Populate ``result`` when ``iris_vector_graph`` imports; Return 1 when ready.
ClassMethod FillIrisVectorGraphProbe(result As %DynamicObject) As %Boolean
{
    Try {
        Set mod = ##class(%SYS.Python).Import("iris_vector_graph")
        If $IsObject(mod) {
            Do result.%Set("ok", 1)
            If 'result.%IsDefined("action") {
                Do result.%Set("action", "present")
            }
            Return 1
        }
    } Catch ex {
        Do result.%Set("import_error", ex.DisplayString())
    }
    Return 0
}

/// Ensure ``iris_vector_graph`` is importable in IRIS embedded Python; optional pip install.
ClassMethod EnsureIrisVectorGraphPythonJSON(pInstall As %Boolean = 0) As %String
{
    Set result = ##class(%DynamicObject).%New()
    Set ready = ..FillIrisVectorGraphProbe(result)
    If ready {
        Return result.%ToJSON()
    }
    If 'pInstall {
        Do result.%Set("ok", 0)
        Do result.%Set("action", "missing")
        Return result.%ToJSON()
    }
    Try {
        Set pipJson = ..PipInstallIrisVectorGraphJson()
        If pipJson '= "" {
            Set pipObj = ##class(%DynamicObject).%FromJSON(pipJson)
            If $IsObject(pipObj) {
                Set pipRc = +pipObj.%Get("pip_rc")
                Do result.%Set("pip_rc", pipRc)
                Set tailOut = pipObj.%Get("pip_stdout_tail")
                If tailOut '= "" {
                    Do result.%Set("pip_stdout_tail", tailOut)
                }
                Set tailErr = pipObj.%Get("pip_stderr_tail")
                If tailErr '= "" {
                    Do result.%Set("pip_stderr_tail", tailErr)
                }
                If pipRc '= 0 {
                    Do result.%Set("ok", 0)
                    Do result.%Set("action", "install_failed")
                    Do result.%Set("error", "pip install failed with rc "_pipRc)
                    Return result.%ToJSON()
                }
            }
        }
    } Catch ex {
        Do result.%Set("ok", 0)
        Do result.%Set("action", "install_failed")
        Do result.%Set("error", ex.DisplayString())
        Return result.%ToJSON()
    }
    Set ready = ..FillIrisVectorGraphProbe(result)
    If ready {
        Do result.%Remove("import_error")
        Do result.%Set("action", "installed")
        Return result.%ToJSON()
    }
    Do result.%Set("ok", 0)
    Do result.%Set("action", "install_failed")
    If 'result.%IsDefined("error") {
        Do result.%Set("error", "import still fails after pip install")
    }
    Return result.%ToJSON()
}

/// Deploy: delete legacy ``Graph.KG.rdfedges`` when present (#5523); Write status lines.
ClassMethod RemediateGraphKgLegacyRdfedgesOutput() As %Status
{
    Set legacy = "Graph.KG.rdfedges"
    If ##class(%Dictionary.CompiledClass).%ExistsId(legacy) {
        Write "Deleting legacy ", legacy, !
        Set sc = $System.OBJ.Delete(legacy, "ck")
        If $System.Status.IsError(sc) {
            Write "ERROR delete: ", $System.Status.GetErrorText(sc), !
            Return sc
        }
        Write "deleted legacy", !
    } Else {
        Write "no legacy", !
    }
    Write "OK", !
    Return $$$OK
}

/// Deploy/install entry: init schema and emit ``InitSchema:`` JSON line for deploy parsing.
ClassMethod DeployInitSchemaOutput() As %Status
{
    Set engine = ..Local()
    Do engine.InitSchema()
    Write "InitSchema: "_engine.InitSchemaJson(),!
    Return $$$OK
}

/// Quick smoke test — initializes schema if needed, runs MATCH (n) RETURN count(n) AS cnt.
ClassMethod SmokeTest()
{
    Set engine = ..Local()
    If 'engine.IsReady() {
        Write "Schema not initialized — running InitSchema()...",!
        Do engine.InitSchema()
    }
    Set r = engine.Query("MATCH (n) RETURN count(n) AS cnt")
    If r.error '= "" { Write "FAIL: "_r.error,! Quit }
    Set cols = r.columns
    If cols.%Size() = 0 { Write "FAIL: no columns",! Quit }
    Set cnt = engine.GetNodeCount("")
    Write "SmokeTest PASSED: "_cnt_" nodes, "_cols.%Size()_" columns",!
}

}
