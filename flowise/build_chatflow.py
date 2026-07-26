#!/usr/bin/env python3
"""Build a 3-retriever Tool-Agent chatflow for the Ukraine-law RAG, cloning node
shapes from the proven 'Humanitarian Assistant v2 (Gemini)' template.

This is the generator that produced the live ``ukr-law-chatflow.json`` export.
It clones exact node/anchor shapes (Flowise flowData is version-sensitive) from a
known-good template flow, then rewires 3 raw-Qdrant retriever tools + a law-first
Tool Agent. To reproduce/regenerate:

  1. Fetch a template Tool-Agent chatflow's JSON into ``template_3d863010.json``
     next to this script:
       curl -A 'Mozilla/5.0' -H "Authorization: Bearer $FLOWISE_API_KEY" \\
         "$FLOWISE_BASE_URL/api/v1/chatflows/<template-id>" > template_3d863010.json
  2. python build_chatflow.py   ->  writes new_flow.json (the flowData object)
  3. PUT new_flow.json's contents (as a JSON *string*) into the live chatflow.

Credential UUIDs below are Flowise credential *references* (not secret values).
"""
import json, os, copy

SP = os.path.dirname(os.path.abspath(__file__))
tpl = json.loads(json.load(open(SP + "/template_3d863010.json"))["flowData"])

GOOGLE_CRED = "3a693377-0f38-4135-b3ad-7d7cbbda504f"
QDRANT_CRED = "6e234d99-85ce-4a80-bf6d-6bbafc44cd64"
QDRANT_URL = "https://qdrant.baena.info"

def tnode(nid):
    return next(n for n in tpl["nodes"] if n["id"] == nid)

def clone(src_id, new_id, position):
    """Deep-clone a template node, rename the id everywhere, set position."""
    n = copy.deepcopy(tnode(src_id))
    s = json.dumps(n)
    s = s.replace(src_id, new_id)          # node id token is unique -> safe
    n = json.loads(s)
    n["position"] = position
    n["positionAbsolute"] = dict(position)
    n["selected"] = False
    n["dragging"] = False
    return n

nodes = []

# 1. Embeddings (shared)
emb = clone("googleGenerativeAiEmbeddings_0", "googleGenerativeAiEmbeddings_0", {"x": -200, "y": 100})
emb["data"]["credential"] = GOOGLE_CRED
emb["data"]["inputs"] = {"modelName": "gemini-embedding-001", "tasktype": "RETRIEVAL_QUERY", "stripNewLines": ""}
nodes.append(emb)

# 2. Chat model
chat = clone("chatGoogleGenerativeAI_0", "chatGoogleGenerativeAI_0", {"x": 700, "y": -260})
chat["data"]["credential"] = GOOGLE_CRED
chat["data"]["inputs"].update({"modelName": "gemini-2.5-flash", "temperature": 0.4, "streaming": True})
nodes.append(chat)

# 3. Buffer memory
mem = clone("bufferMemory_0", "bufferMemory_0", {"x": 700, "y": 120})
mem["data"]["inputs"] = {"sessionId": "", "memoryKey": "chat_history"}
nodes.append(mem)

# 4-6. Three Qdrant retrievers
QDRANTS = [
    ("qdrant_0", "rada_legislation", 5, {"x": 150, "y": -300}),
    ("qdrant_1", "curated_legislation", 4, {"x": 150, "y": 120}),
    ("qdrant_2", "secondary_reports", 3, {"x": 150, "y": 540}),
]
for nid, col, topk, pos in QDRANTS:
    q = clone("qdrant_0", nid, pos)
    q["data"]["credential"] = QDRANT_CRED
    q["data"]["inputs"] = {
        "document": [],
        "embeddings": "{{googleGenerativeAiEmbeddings_0.data.instance}}",
        "recordManager": "",
        "qdrantServerUrl": QDRANT_URL,
        "qdrantCollection": col,
        "fileUpload": "",
        "qdrantVectorDimension": "3072",
        "contentPayloadKey": "text",
        "metadataPayloadKey": "metadata",
        "batchSize": "",
        "qdrantSimilarity": "Cosine",
        "qdrantCollectionConfiguration": "",
        "topK": topk,
        "qdrantFilter": "",
    }
    nodes.append(q)

# 7-9. Three retriever tools
TOOLS = [
    ("retrieverTool_0", "qdrant_0", "search_primary_legislation",
     "Search PRIMARY Ukrainian legislation (Verkhovna Rada laws, Cabinet resolutions, decrees) — the authoritative statutory text. Use this FIRST for any legal question. Input a focused search query in the user's language.",
     {"x": 480, "y": -300}),
    ("retrieverTool_1", "qdrant_1", "search_curated_humanitarian_law",
     "Search the CURATED humanitarian-law knowledge base: laws and provisions hand-selected for NGOs operating in Ukraine (IDPs, martial law, humanitarian aid, taxation, data protection). Use to enrich answers with humanitarian-specific statutory detail. Input a focused query.",
     {"x": 480, "y": 120}),
    ("retrieverTool_2", "qdrant_2", "search_secondary_analysis",
     "Search SECONDARY expert analyses/commentary that review Ukrainian law (reports, comparisons, reform proposals). This is NOT the law itself — use only for interpretation, context, or to flag proposed changes, and always label it as analysis with its date. Input a focused query.",
     {"x": 480, "y": 540}),
]
for nid, qref, name, desc, pos in TOOLS:
    rt = clone("retrieverTool_0", nid, pos)
    rt["data"]["inputs"] = {
        "name": name,
        "description": desc,
        "retriever": "{{%s.data.instance}}" % qref,
        "returnSourceDocuments": True,
        "retrieverToolMetadataFilter": "",
    }
    nodes.append(rt)

# 10. Tool agent
SYSTEM = """You are a legal research assistant specializing in Ukrainian legislation, serving humanitarian NGOs operating in Ukraine.

TOOLS — you have three search tools. Use them, do not answer legal questions from memory:
- search_primary_legislation → authoritative Rada laws/resolutions. ALWAYS search this first.
- search_curated_humanitarian_law → humanitarian-focused statutory provisions (IDPs, martial law, aid, tax, data protection).
- search_secondary_analysis → expert commentary REVIEWING the law. This is NOT the law.

METHOD:
1. For any legal question, search primary legislation first, then the curated humanitarian base. Search analysis only if interpretation, context, or reform status is relevant.
2. Base your answer strictly on retrieved excerpts. If they don't fully answer, say so — never invent a law, article number, or date.
3. Treat PRIMARY and CURATED results as authoritative law. Treat SECONDARY ANALYSIS as commentary only: never present it as the law, always mark it as analysis and give its date (analyses go out of date).
4. Cite the specific law title, law id, and article/section, with the source URL when available.
5. Note enactment dates, and flag martial-law context where it affects the answer.
6. Answer in the user's language (English or Ukrainian). Be precise about rights, obligations, and procedures.
7. You are not a lawyer; add a brief note to verify with qualified counsel for consequential decisions."""

agent = clone("toolAgent_0", "toolAgent_0", {"x": 1050, "y": 100})
agent["data"]["inputs"] = {
    "tools": ["{{retrieverTool_0.data.instance}}",
              "{{retrieverTool_1.data.instance}}",
              "{{retrieverTool_2.data.instance}}"],
    "memory": "{{bufferMemory_0.data.instance}}",
    "model": "{{chatGoogleGenerativeAI_0.data.instance}}",
    "chatPromptTemplate": "",
    "systemMessage": SYSTEM,
    "inputModeration": [],
    "maxIterations": "",
    "enableDetailedStreaming": False,
}
nodes.append(agent)

# ---- Edges ----
def edge(src, sh, tgt, th):
    return {"source": src, "sourceHandle": sh, "target": tgt, "targetHandle": th,
            "type": "buttonedge", "id": f"{src}-{sh}-{tgt}-{th}"}

EMB_OUT = "googleGenerativeAiEmbeddings_0-output-googleGenerativeAiEmbeddings-GoogleGenerativeAiEmbeddings|GoogleGenerativeAIEmbeddings|Embeddings"
edges = []
for nid, _, _, _ in QDRANTS:
    edges.append(edge("googleGenerativeAiEmbeddings_0", EMB_OUT, nid, f"{nid}-input-embeddings-Embeddings"))
for nid, qref, _, _, _ in TOOLS:
    edges.append(edge(qref, f"{qref}-output-retriever-Qdrant|VectorStoreRetriever|BaseRetriever",
                      nid, f"{nid}-input-retriever-BaseRetriever"))
    edges.append(edge(nid, f"{nid}-output-retrieverTool-RetrieverTool|DynamicTool|Tool|StructuredTool|Runnable",
                      "toolAgent_0", "toolAgent_0-input-tools-Tool"))
edges.append(edge("chatGoogleGenerativeAI_0",
                  "chatGoogleGenerativeAI_0-output-chatGoogleGenerativeAI-ChatGoogleGenerativeAI|LangchainChatGoogleGenerativeAI|BaseChatModel|BaseLanguageModel|Runnable",
                  "toolAgent_0", "toolAgent_0-input-model-BaseChatModel"))
edges.append(edge("bufferMemory_0",
                  "bufferMemory_0-output-bufferMemory-BufferMemory|BaseChatMemory|BaseMemory",
                  "toolAgent_0", "toolAgent_0-input-memory-BaseChatMemory"))

flow = {"nodes": nodes, "edges": edges, "viewport": {"x": 0, "y": 0, "zoom": 0.6}}
out = SP + "/new_flow.json"
json.dump(flow, open(out, "w"), ensure_ascii=False, indent=1)

# sanity checks
node_ids = {n["id"] for n in nodes}
assert len(node_ids) == len(nodes), "duplicate node ids"
for e in edges:
    assert e["source"] in node_ids and e["target"] in node_ids, f"dangling edge {e['id']}"
# verify every {{ref}} in inputs points to a real node
import re
refs = set(re.findall(r"\{\{(\w+)\.data\.instance\}\}", json.dumps(flow)))
missing = refs - node_ids
assert not missing, f"inputs reference missing nodes: {missing}"
print(f"OK: {len(nodes)} nodes, {len(edges)} edges, refs valid -> {out}")
print("nodes:", [n['id'] for n in nodes])
