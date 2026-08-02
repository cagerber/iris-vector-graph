/// User.CypherFn — SQL UDFs for Cypher expression translation
/// All method names use IVG prefix to avoid IRIS 2026.3 reserved keyword conflicts.
/// SQL projection names: SQLUser.CypherFn_IVG<Name>
Class User.CypherFn Extends %RegisteredObject
{

/// Regex match operator (=~ in Cypher). Returns 1 if string matches pattern, 0 otherwise.
ClassMethod IVGREGEXMATCH(s As %String = "", p As %String = "") As %Integer [ SqlProc ]
{
    If s = "" || p = "" { Return 0 }
    Try {
        Set m = ##class(%Regex.Matcher).%New(p)
        Set r = m.Match(s)
        Return $Select(r: 1, 1: 0)
    } Catch { Return 0 }
}

/// JSON array length. Returns 0 for null/empty.
ClassMethod IVGJSONARRAYLENGTH(json As %String = "") As %Integer [ SqlProc ]
{
    If json = "" || json = "null" { Return 0 }
    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" { Return 0 }
        Return arr.%Size()
    } Catch { Return 0 }
}

/// JSON value extraction via JSONPath (supports $.key and $[N] forms).
ClassMethod IVGJSONVALUE(json As %String = "", path As %String = "") As %String [ SqlProc ]
{
    If json = "" || json = "null" { Return "" }
    Try {
        Set obj = ##class(%DynamicObject).%FromJSON(json)
        If obj = "" { Return "" }
        If $Extract(path, 1, 2) = "$." {
            Return obj.%Get($Extract(path, 3, *))
        } ElseIf $Extract(path, 1, 2) = "$[" {
            Set idx = +$Extract(path, 3, $Length(path) - 1)
            If obj.%IsA("%DynamicArray") { Return obj.%Get(idx) }
        }
        Return ""
    } Catch { Return "" }
}

/// Return keys of a JSON object as a JSON array string.
ClassMethod IVGJSONKEYS(json As %String = "") As %String [ SqlProc ]
{
    If json = "" || json = "null" { Return "[]" }
    Try {
        Set obj = ##class(%DynamicObject).%FromJSON(json)
        If obj = "" { Return "[]" }
        Set keys = ##class(%DynamicArray).%New()
        Set iter = obj.%GetIterator()
        While iter.%GetNext(.key, .val) { Do keys.%Push(key) }
        Return keys.%ToJSON()
    } Catch { Return "[]" }
}

/// Reverse a JSON array.
ClassMethod IVGLISTREVERSE(json As %String = "") As %String [ SqlProc ]
{
    If json = "" || json = "null" { Return "[]" }
    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" { Return "[]" }
        Set rev = ##class(%DynamicArray).%New()
        Set sz = arr.%Size()
        For i = sz:-1:1 { Do rev.%Push(arr.%Get(i - 1)) }
        Return rev.%ToJSON()
    } Catch { Return "[]" }
}

/// Split string by delimiter and return as JSON array.
ClassMethod IVGSTRSPLIT(s As %String = "", delim As %String = "") As %String [ SqlProc ]
{
    If s = "" { Return "[]" }
    Set result = ##class(%DynamicArray).%New()
    Set pieces = $Length(s, delim)
    For i = 1:1:pieces { Do result.%Push($Piece(s, delim, i)) }
    Return result.%ToJSON()
}

/// Generate a random UUID.
ClassMethod IVGNEWID() As %String [ SqlProc ]
{
    Return ##class(%SYS.Util).CreateGUID()
}

/// Generate a random float in [0, 1).
ClassMethod IVGRAND() As %Decimal [ SqlProc ]
{
    Return ##class(%PopulationStatistics).RandomDouble()
}

/// Get first element of a JSON array.
ClassMethod IVGLISTHEAD(json As %String = "") As %String [ SqlProc ]
{
    If json = "" || json = "null" { Return "" }
    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" || arr.%Size() = 0 { Return "" }
        Return arr.%Get(0)
    } Catch { Return "" }
}

/// Get last element of a JSON array.
ClassMethod IVGLISTLAST(json As %String = "") As %String [ SqlProc ]
{
    If json = "" || json = "null" { Return "" }
    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" || arr.%Size() = 0 { Return "" }
        Return arr.%Get(arr.%Size() - 1)
    } Catch { Return "" }
}

/// Get all but the first element of a JSON array.
ClassMethod IVGLISTTAIL(json As %String = "") As %String [ SqlProc ]
{
    If json = "" || json = "null" { Return "[]" }
    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" || arr.%Size() = 0 { Return "[]" }
        Set tail = ##class(%DynamicArray).%New()
        For i = 1:1:arr.%Size() - 1 { Do tail.%Push(arr.%Get(i)) }
        Return tail.%ToJSON()
    } Catch { Return "[]" }
}

}
