import frappe
import requests as py_requests
from werkzeug.exceptions import HTTPException
from werkzeug.wrappers import Response as WZResponse

class ProxyResponseException(HTTPException):
    def __init__(self, content, status_code, headers):
        super().__init__()
        self.content = content
        self.status_code = status_code
        self.headers_list = headers

    def get_response(self, environ=None):
        return WZResponse(
            response=self.content,
            status=self.status_code,
            headers=self.headers_list
        )

def before_request():
    request = frappe.local.request
    if not request:
        return

    # Check if request comes from python-httpx (the AI calling backend)
    user_agent = request.headers.get("User-Agent") or ""
    
    # We also check path for ai_calling / lead_management just in case the User-Agent was overridden/missing
    is_ai_calling = (
        request.path.startswith("/api/method/ai_calling.")
        or request.path.startswith("/api/method/lead_management.")
        or request.path.startswith("/api/method/msuite_workspace.")
        # Canonical home of the lead check API (check_exists moved from
        # msuite_workspace.api.v1.lead to paideia_crm.api.v1.lead; the
        # old dotted path still works as a shim, but new integrations
        # use the paideia path).
        or request.path.startswith("/api/method/paideia_crm.")
    )
    is_httpx = ("httpx" in user_agent.lower() or "python-httpx" in user_agent.lower()) and not request.path.startswith("/api/method/login")

    if is_ai_calling or is_httpx:
        # Check if the active client has a registered client_url in MSuite Client
        client_name = frappe.db.get_value("MSuite Client", {"status": "Active"}, "name")
        api_key = ""
        api_secret = ""
        client_url = "http://localhost:8001"
        if client_name:
            client_doc = frappe.get_doc("MSuite Client", client_name)
            client_url = client_doc.client_url or "http://localhost:8001"
            api_key = client_doc.api_key or ""
            api_secret = client_doc.get_password("api_secret") or ""

        client_url = client_url.rstrip("/")
        target_url = f"{client_url}{request.path}"
        if request.query_string:
            target_url += f"?{request.query_string.decode('utf-8')}"

        headers = {key: value for key, value in request.headers.items() if key.lower() not in ("host", "content-length")}
        # Remove any leading protocols from client_url to construct correct Host header
        host_header = client_url.replace("http://", "").replace("https://", "")
        headers["Host"] = host_header

        # Inject provider credentials for the client auth hook to validate
        if api_key and api_secret:
            headers["X-MSuite-Provider-Key"] = api_key
            headers["X-MSuite-Provider-Secret"] = api_secret

        try:
            # Proxy request to the client site
            if request.method == "POST":
                resp = py_requests.post(
                    target_url,
                    headers=headers,
                    data=request.get_data(),
                    timeout=30
                )
            elif request.method == "GET":
                resp = py_requests.get(
                    target_url,
                    headers=headers,
                    timeout=30
                )
            else:
                resp = py_requests.request(
                    request.method,
                    target_url,
                    headers=headers,
                    data=request.get_data(),
                    timeout=30
                )

            # Filter out hop-by-hop and duplicate headers
            hop_by_hop = ("content-encoding", "transfer-encoding", "content-length", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "upgrade", "server", "date")
            headers_list = [(k, v) for k, v in resp.headers.items() if k.lower() not in hop_by_hop]

            raise ProxyResponseException(resp.content, resp.status_code, headers_list)
        except HTTPException:
            raise
        except Exception as proxy_exc:
            frappe.logger().error(f"[Proxy Exception] Failed to proxy {request.path} to {client_url}: {proxy_exc}")
