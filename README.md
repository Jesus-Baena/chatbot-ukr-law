# chatbot-ukr-law

## Overview

The AI Agent UKR Legal Adviser is a personal solution designed to provide preliminary legal advice and legislative information before incurring on legal fees, tailored for Ukrainian NGOs involved in humanitarian operations. This project combines a modern, responsive chat interface with a robust backend workflow (powered by n8n) that integrates advanced AI language models, document retrieval systems, and multiple external data sources to deliver accurate, context-aware legal guidance.
Features

  Interactive Chat Interface
    A user-friendly, responsive web-based chat application built with HTML, CSS, and JavaScript. It supports Markdown rendering for enhanced message formatting.

  AI-Powered Legal Advising
    Leverages state-of-the-art language models (OpenAI Chat Model, Google Gemini) to process user queries and generate detailed legal responses.

  Dynamic Document Retrieval
    Utilizes vector search (via Supabase) and contextual memory (using Postgres) to fetch relevant legislation and legal documents.

  Automated Document Ingestion & Processing
    Integrates with Google Drive and Google Sheets to automatically ingest, parse, and update legislative documents, with additional translation support via DeepL.

  External API Integrations
    Connects to multiple external APIs (SerpAPI, ACAPS, Llama Cloud Parser, etc.) to enrich responses with up-to-date data and context.

## Architecture

The project consists of two primary components:

  Frontend (Chat Interface):
        A single-page application that handles user interactions.
        Sends user queries via a POST request to the n8n webhook endpoint.

  Backend (n8n Workflows):
        A series of interconnected nodes that process incoming messages.
        Integrates AI models, external data sources, document processing tools, and databases.
        Maintains conversation context using a Postgres-based chat memory node.

## Technologies Used

  Frontend:
        HTML5, CSS3, JavaScript
        Marked.js for Markdown parsing

  Backend:
        n8n for workflow automation
        AI models: OpenAI Chat Model & Google Gemini Chat Model
        External APIs: SerpAPI, ACAPS, Llama Cloud Parser, DeepL
        Databases: Postgres (for chat memory) and Supabase (for vector store)
        Integrations: Google Drive (document triggers) and Google Sheets (data export)

## Setup and Installation
### Prerequisites

  Web Server: To host the frontend HTML file.
  n8n Instance: To run and manage backend workflows.
  API keys/credentials for:
        OpenAI (or your chosen AI provider)
        Google Gemini / Google PaLM API
        SerpAPI
        Postgres database
        Supabase account
        Google Drive and Google Sheets APIs
        DeepL API
        Llama Cloud Parser
        ACAPS API

### Frontend Setup

  Host the HTML File:
    Place the provided index.html (or similar) file on your web server. Ensure it is accessible to users.

  Dependencies:
    The chat interface uses the Marked.js library via CDN. Verify that your internet connection allows access to the CDN, or host the library locally if necessary.

  Configuration:
    The JavaScript in the HTML file sends a POST request to https://jbaena.app.n8n.cloud/webhook/ukr-law-adviser. If you are hosting your own n8n instance, update this URL accordingly.

### Backend (n8n) Setup

  Import Workflow:
    Import the provided n8n workflow JSON file into your n8n instance. This workflow contains all nodes necessary for:
        Receiving chat messages via a webhook.
        Processing queries with AI and external tools.
        Managing document retrieval and context memory.
        Integrating with Google Sheets, Supabase, Postgres, and other services.

  Configure Credentials:
    Set up and assign credentials in n8n for each service:
        OpenAI API / Google Gemini API
        SerpAPI
        Postgres
        Supabase
        Google Drive & Google Sheets (OAuth2)
        DeepL
        Llama Cloud Parser
        ACAPS API

  ### Adjust Node Settings:
  Review the node configurations (e.g., webhook paths, API endpoints, custom prompts) and adjust them as needed for your environment.

  ### Activate Workflow:
  Once configured, activate the workflow to enable processing of incoming chat messages.

## Usage

  Access the Chat Interface:
    Open the hosted HTML page in your browser [jbaena.net/](https://jbaena.net/portfolio/chatbot-ukr-law)

  Enter a Query:
    Type your question related to Ukrainian legislation into the chat input field and press "Send".

  Receive a Response:
    Your query is forwarded to the n8n backend where it is processed by the AI agent. The response, enriched with relevant legal data and document references, will appear in the chat window.

  Conversation Context:
    The system maintains conversation context using Postgres-based chat memory, ensuring responses remain relevant across multiple interactions.

## Workflow Details

  Webhook Node:
    Receives user messages and triggers the workflow.

  AI Agent Node:
    Processes queries using AI models (OpenAI, Google Gemini) and interacts with external tools like SerpAPI and ACAPS.

  Memory Integration:
    Utilizes a Postgres chat memory node to store and manage conversation context.

  Document Retrieval & Processing:
        Retrieves documents via Supabase Vector Store.
        Processes legal documents using Llama Cloud Parser and translates content with DeepL.
        Updates legislative metadata in Google Sheets.

  Data Persistence:
    Inserts or updates processed document data into Supabase and Postgres for future reference and semantic search.

## Contributing

Contributions are welcome! If you have improvements, bug fixes, or additional features, please fork the repository and submit a pull request. For major changes, please open an issue first to discuss what you would like to change.

MIT License.

## Acknowledgements

  Thanks to the n8n community for their support.
  Special thanks to the providers of the external APIs and services (OpenAI, Google, SerpAPI, ACAPS, DeepL, etc.).
  Gratitude to the Ukrainian NGO community and legal experts for inspiring this project.
