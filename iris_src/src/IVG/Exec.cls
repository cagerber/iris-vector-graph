/// IVG.Exec — Minimal ObjectScript execution helper for Python SDK.
/// Allows Python to run arbitrary ObjectScript via classMethodVoid.
/// Renamed from User.Exec (trifour.5): User package triggers Config.MapPackages
/// SUBSCRIPT on IRIS Health when %IPM maps to USER database.
Class IVG.Exec Extends %RegisteredObject
{

ClassMethod Run(cmd As %String) As %String
{
    Xecute cmd
    Return ""
}

}
