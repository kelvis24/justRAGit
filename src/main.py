import argparse
import os

import dotenv
from langchain_community.chat_models import ChatOpenAI
from langchain_community.document_loaders import PyPDFLoader
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_text_splitters import RecursiveCharacterTextSplitter

import database
client = database.create_client()
if client.is_ready():
    print("Weaviate is ready!")
else:
    print("Weaviate is not ready!")

collection_name = "Article"
api_key = os.getenv("OPENAI_API_KEY")

import requests


def embed_text(text):
    global api_key  # Replace with your OpenAI API key
    url = "https://api.openai.com/v1/embeddings"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    data = {
        "model": "text-embedding-3-small",  # Specify the model to use
        "input": text,

    }

    response = requests.post(url, headers=headers, json=data)
    if response.status_code == 200:
        embeddings = response.json()['data']
        return embeddings
    else:
        print("Failed to get embeddings:", response.status_code, response.text)


dotenv.load_dotenv()
os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")


def docLoader(filename):
    loader = PyPDFLoader(filename, extract_images=True)
    documents = loader.load()
    return documents


def chunkRecursively(text, chunk_size, chunk_overlap):
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        length_function=len,
        is_separator_regex=False,
    )

    res = text_splitter.create_documents([text])
    return res


def index(filename, chunk_size, chunk_overlap):
    global collection_name
    chunked_doc = []
    for _, doc in enumerate(docLoader(filename)):
        chunked_doc.append(chunkRecursively(doc.page_content, chunk_size, chunk_overlap))  # 25% overlap

    vector_store = []
    for item in chunked_doc:
        for doc in item:
            vector_store.append(doc.page_content)

    chunk_class = {
        "class": collection_name,
        "properties": [
            {
                "name": "chunk",
                "dataType": ["text"],
            },
            {
                "name": "chunk_index",
                "dataType": ["int"],
            }
        ],
        "vectorizer": "text2vec-openai",  # Use `text2vec-openai` as the vectorizer
        "moduleConfig": {
            "text2vec-openai": {
                "model": "text-embedding-3-small",
                "dimensions": 1536,  # Example model, specify the model you have access to
                "tokenization": True,  # Enables tokenization if necessary
            }  # Use `generative-openai` with default parameters
        }
    }

    if client.schema.exists(collection_name):  # In case we've created this collection before
        client.schema.delete_class(collection_name)
    client.schema.create_class(chunk_class)

    client.batch.configure(batch_size=100)
    with client.batch as batch:
        for i, chunk in enumerate(vector_store):
            data_object = {
                "chunk": chunk,
                "chunk_index": i
            }
            batch.add_data_object(data_object=data_object, class_name=collection_name)

    print("Data indexed successfully!")
    response = client.query.aggregate(collection_name).with_meta_count().do()
    print(response)


# TOP-K QUERIES
'''
def queries(query: str, top_k: int):
    global collection_name
    q_embeddings = {
        'vector': embed_text(query)[0]['embedding']
    }
    context = ""
    result = client.query.get(collection_name, ["chunk"]).with_near_vector(q_embeddings).with_limit(
        top_k).with_additional(['certainty']).do()
    for res in result['data']['Get']['Article']:
        context += res['chunk'] + "\n\n"
    return context
'''


#function returns query after reranking
def queries(query: str, top_k: int, top_p: int):
    global collection_name
    q_embeddings = {'vector': embed_text(query)[0]['embedding']}
    result = client.query.get(collection_name, ["chunk"]).with_near_vector(q_embeddings).with_limit(
        top_k).with_additional(['certainty']).do()

    results_with_scores = []
    try:
        for res in result['data']['Get'][collection_name]:
            chunk = res['chunk']
            # Check if 'additional' and 'certainty' keys are present
            certainty = res.get('additional', {}).get('certainty', 0)  # Default certainty to 0 if not found
            results_with_scores.append((chunk, certainty))
    except KeyError as e:
        print(f"Key error encountered: {e}")
        return "Failed to process results due to key error."

    sorted_results = sorted(results_with_scores, key=lambda x: x[1], reverse=True)
    top_p_results = sorted_results[:top_p]
    context = "\n\n".join([res[0] for res in top_p_results])

    return context


# Answering queries
def answer_query(query: str, context: str):
    template = """You are an assistant for question-answering tasks. Use the following pieces of retrieved context to answer the question.\
          If you don't know the answer, just say that you don't know. Use three sentences maximum and keep the answer concise.
    Question: {question}
    Context: {context}
    Answer:
    """
    prompt = ChatPromptTemplate.from_template(template)
    llm = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)

    rag_chain = (
            {"context": lambda x: context, "question": RunnablePassthrough()}
            | prompt
            | llm
            | StrOutputParser()
    )

    return rag_chain.invoke(f"{query}")


def args():
    parser = argparse.ArgumentParser(description="Upload and Index file to Weaviate")
    parser.add_argument("--pdf_file", help="Input file", required=False, type=str)
    parser.add_argument("--chunk_size", help="Chunk size", type=int, default=1000)
    parser.add_argument("--chunk_overlap", help="Chunk overlap", type=int, default=250)
    parser.add_argument("--top_k", help="Top K results", type=int, default=5)
    return parser.parse_args()


if __name__ == "__main__":
    args = args()
    # print(args)
    if args.pdf_file:
        index(args.pdf_file, args.chunk_size, args.chunk_overlap)
    else:
        print("No input file provided")
        exit(1)

    while True:
        query = input("Enter your query (or 'q' to quit): ")
        if query == 'q':
            print("Exiting...")
            break

        context = queries(query, args.top_k, top_p=4)
        print(answer_query(query, context))
