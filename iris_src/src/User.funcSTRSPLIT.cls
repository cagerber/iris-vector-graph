/// User.funcSTRSPLIT — SQL UDF for Cypher split() function
/// SQL projection name: SQLUser.STR_SPLIT
/// Note: method name cannot be STR_SPLIT (reserved keyword in IRIS 2026.3);
/// use SqlName annotation to project as STR_SPLIT.
Class User.funcSTRSPLIT Extends %RegisteredObject
{

/// Split string by delimiter and return as JSON array.
ClassMethod STRSPLIT(s As %String = "", delim As %String = "") As %String [ SqlName = STR_SPLIT, SqlProc ]
{
    If s = "" { Return "[]" }
    Set result = ##class(%DynamicArray).%New()
    Set pieces = $Length(s, delim)
    For i = 1:1:pieces { Do result.%Push($Piece(s, delim, i)) }
    Return result.%ToJSON()
}

}
