/// User.SQL - SQL UDFs for Cypher expression translation
/// Provides regex matching, string utilities, and JSON operations for TCK test compatibility
Class User.SQL Extends %RegisteredObject
{

/// Regex match operator (=~ in Cypher)
/// Returns 1 if string matches regex pattern, 0 otherwise
ClassMethod REGEX_MATCH(str As %String, pattern As %String) As %Integer
{
    If str = "" || pattern = "" {
        Return 0
    }


    Try {
        Set compiled = ##class(%Regex.Matcher).%New(pattern)
        Set result = compiled.Match(str)
        Return $Select(result: 1, 1: 0)
    } Catch ex {

        Return 0
    }
}

/// JSON array length
ClassMethod JSON_ARRAYLENGTH(json As %String) As %Integer
{
    If json = "" || json = "null" {
        Return 0
    }

    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" {
            Return 0
        }
        Return arr.%Size()
    } Catch {
        Return 0
    }
}

/// JSON value extraction via JSONPath
ClassMethod JSON_VALUE(json As %String, path As %String) As %String
{
    If json = "" || json = "null" {
        Return ""
    }

    Try {
        Set obj = ##class(%DynamicObject).%FromJSON(json)
        If obj = "" {
            Return ""
        }


        If $Extract(path, 1, 2) = "$." {
            Set key = $Extract(path, 3, *)
            Return obj.%Get(key)
        } ElseIf $Extract(path, 1, 2) = "$[" {
            Set idx = +$Extract(path, 3, $Length(path) - 1)
            If obj.%IsA("%DynamicArray") {
                Return obj.%Get(idx)
            }
        }
        Return ""
    } Catch {
        Return ""
    }
}

/// Reverse a JSON array
ClassMethod LIST_REVERSE(json As %String) As %String
{
    If json = "" || json = "null" {
        Return "[]"
    }

    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" {
            Return "[]"
        }

        Set reversed = ##class(%DynamicArray).%New()
        Set size = arr.%Size()
        For i = size:-1:1 {
            Do reversed.%Push(arr.%Get(i - 1))
        }
        Return reversed.%ToJSON()
    } Catch {
        Return "[]"
    }
}

/// Split string by delimiter and return as JSON array
ClassMethod STR_SPLIT(str As %String, delimiter As %String) As %String
{
    If str = "" {
        Return "[]"
    }

    Set result = ##class(%DynamicArray).%New()
    Set pieces = $Length(str, delimiter)

    For i = 1:1:pieces {
        Set piece = $Piece(str, delimiter, i)
        Do result.%Push(piece)
    }

    Return result.%ToJSON()
}

/// Generate random UUID
ClassMethod NEWID() As %String
{
    Return ##class(%SYS.Util).CreateGUID()
}

/// Generate random float [0,1)
ClassMethod RAND() As %Decimal
{
    Return ##class(%PopulationStatistics).RandomDouble()
}

/// Get first element of JSON array
ClassMethod LIST_HEAD(json As %String) As %String
{
    If json = "" || json = "null" {
        Return ""
    }

    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" || arr.%Size() = 0 {
            Return ""
        }
        Return arr.%Get(0)
    } Catch {
        Return ""
    }
}

/// Get last element of JSON array
ClassMethod LIST_LAST(json As %String) As %String
{
    If json = "" || json = "null" {
        Return ""
    }

    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" || arr.%Size() = 0 {
            Return ""
        }
        Return arr.%Get(arr.%Size() - 1)
    } Catch {
        Return ""
    }
}

/// Get all but first element of JSON array
ClassMethod LIST_TAIL(json As %String) As %String
{
    If json = "" || json = "null" {
        Return "[]"
    }

    Try {
        Set arr = ##class(%DynamicArray).%FromJSON(json)
        If arr = "" || arr.%Size() = 0 {
            Return "[]"
        }

        Set tail = ##class(%DynamicArray).%New()
        For i = 1:1:arr.%Size() - 1 {
            Do tail.%Push(arr.%Get(i))
        }
        Return tail.%ToJSON()
    } Catch {
        Return "[]"
    }
}

}
