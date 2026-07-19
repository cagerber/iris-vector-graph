Class Graph.KG.kgNodeEmbeddingsoptimized Extends %Persistent [ DdlAllowed, Final, SqlTableName = kg_NodeEmbeddings_optimized ]
{

Property id As %String(COLLATION = "EXACT", MAXLEN = 256) [ Required, SqlColumnNumber = 2, SqlFieldName = id ];

Property emb As %Library.Vector(DATATYPE = "double", LEN = 384) [ SqlColumnNumber = 3, SqlFieldName = emb ];

Property metadata As %Library.DynamicObject [ SqlColumnNumber = 4, SqlFieldName = metadata ];

Index IDKEY On id [ IdKey, PrimaryKey, Unique ];

}
