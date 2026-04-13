# Middleware Module

HTTP middleware components for the OpenNVR backend. Middleware runs on **every HTTP request** before reaching route handlers, providing cross-cutting concerns like logging, monitoring, and request tracking.

---

## 📁 **Files**

### **`request_logging.py`**
**Comprehensive HTTP request/response tracking and monitoring middleware**

**What it does:**

#### **1. Request/Response Logging**
- Logs every incoming HTTP request (method, URL, headers, query params)
- Logs every outgoing HTTP response (status code, headers)
- Sanitizes sensitive data (Authorization headers, cookies, API keys)

#### **2. Performance Monitoring**
- Measures request processing time (start → response)
- Tracks slow requests for performance analysis
- Helps identify bottlenecks in API endpoints

#### **3. Request Correlation**
- Generates unique `request_id` (UUID) for each request
- Adds `X-Request-ID` header to every response
- Allows tracing a single request through distributed logs
- Essential for debugging and troubleshooting

#### **4. Error Tracking**
- Captures exceptions and errors during request processing
- Logs full stack traces with context (URL, method, client IP)
- Includes request metadata in error logs

#### **5. Audit Trail**
- Records all API calls for compliance and security
- Includes client IP address and user agent
- Logs request/response pairs for forensic analysis

---

## 🔧 **How It Works**

**Execution Flow:**
```
Client Request
    ↓
CORSMiddleware (allow origins, headers)
    ↓
RequestLoggingMiddleware ←── YOU ARE HERE
    ↓
Route Handler (your API endpoint)
    ↓
Response
    ↓
RequestLoggingMiddleware (log response)
    ↓
Client Response
```

**Timing:**
- **Before route handler:** Generates request ID, logs incoming request
- **After route handler:** Measures time, logs response, adds headers
- **On exception:** Logs error with full context

---

## 📊 **Example Log Output**

### **Successful Request:**
```json
{
  "timestamp": "2026-02-23T10:30:45.123Z",
  "level": "INFO",
  "action": "api.request_start",
  "message": "GET /api/v1/cameras",
  "method": "GET",
  "url": "http://localhost:8000/api/v1/cameras?limit=10",
  "path": "/api/v1/cameras",
  "query_params": {"limit": "10"},
  "headers": {"authorization": "[REDACTED]", "user-agent": "..."},
  "client_host": "192.168.1.100",
  "request_id": "a3c5f7b9-2e4d-4f8a-9b1c-3d5e7f9a1b2c"
}

{
  "timestamp": "2026-02-23T10:30:45.168Z",
  "level": "INFO",
  "action": "api.request_complete",
  "message": "GET /api/v1/cameras - 200",
  "method": "GET",
  "path": "/api/v1/cameras",
  "status_code": 200,
  "process_time_seconds": 0.045,
  "request_id": "a3c5f7b9-2e4d-4f8a-9b1c-3d5e7f9a1b2c"
}
```

### **Failed Request:**
```json
{
  "timestamp": "2026-02-23T10:35:12.456Z",
  "level": "ERROR",
  "message": "Request failed: POST /api/v1/cameras",
  "method": "POST",
  "path": "/api/v1/cameras",
  "status_code": 500,
  "process_time_seconds": 0.125,
  "exception_type": "DatabaseConnectionError",
  "exception": "Traceback (most recent call last)...",
  "request_id": "b7d9e2f1-3a4c-5d6e-8f9a-0b1c2d3e4f5a"
}
```

---

## 🔐 **Security Features**

### **Header Sanitization**
Sensitive headers are automatically redacted in logs:
- `Authorization` → `[REDACTED]`
- `Cookie` → `[REDACTED]`
- `X-API-Key` → `[REDACTED]`
- `X-Auth-Token` → `[REDACTED]`

**Why?** Prevents credential leakage in log files that may be shared with support teams or stored in centralized logging systems.

### **Client Tracking**
- Logs client IP address for security monitoring
- Tracks user agents for client identification
- Helps detect suspicious activity patterns

---

## 📈 **Performance Impact**

**Overhead per request:**
- Request ID generation: ~0.01ms (UUID v4)
- Header sanitization: ~0.05ms (dictionary copy + filter)
- Logging: ~0.1-0.3ms (depends on log backend)
- **Total:** < 0.5ms per request

**For an NVR system handling 10-50 requests/sec, this overhead is negligible.**

---

## 🎯 **Use Cases**

### **1. Debugging Production Issues**
```bash
# Find all requests from a specific client
cat logs/server.log | grep "192.168.1.100"

# Trace a specific request by ID
cat logs/server.log | grep "a3c5f7b9-2e4d-4f8a-9b1c"

# Find slow requests (> 1 second)
cat logs/server.log | jq 'select(.process_time_seconds > 1)'
```

### **2. Performance Analysis**
```bash
# Find slowest endpoints
cat logs/server.log | jq -r 'select(.action == "api.request_complete") | "\(.process_time_seconds) \(.path)"' | sort -rn | head -20
```

### **3. Security Monitoring**
```bash
# Find all failed authentication attempts
cat logs/server.log | jq 'select(.status_code == 401)'

# Track requests from suspicious IP
cat logs/server.log | grep "suspicious.ip.address"
```

### **4. API Usage Statistics**
```bash
# Count requests per endpoint
cat logs/server.log | jq -r 'select(.action == "api.request_complete") | .path' | sort | uniq -c | sort -rn
```

---

## 🧪 **Configuration**

The middleware is automatically applied in [`main.py`](../main.py):

```python
from middleware import RequestLoggingMiddleware

# Add request logging middleware
app.add_middleware(RequestLoggingMiddleware)
```

**Order matters!** Middleware runs in reverse order of addition:
```python
# 1. Added first (runs LAST)
app.add_middleware(CORSMiddleware, ...)

# 2. Added second (runs FIRST)
app.add_middleware(RequestLoggingMiddleware)

# Execution order:
# Request → RequestLoggingMiddleware → CORSMiddleware → Route Handler
```

---

## 🔄 **Request ID Propagation**

The `X-Request-ID` header enables distributed tracing:

```
Client → Backend → MediaMTX → AI Service
         [req-123] [req-123]   [req-123]
```

**Usage in route handlers:**
```python
@router.get("/cameras")
async def get_cameras(request: Request):
    request_id = request.headers.get("X-Request-ID")
    # Pass to external services for correlation
    await external_service.call(headers={"X-Request-ID": request_id})
```

---

## 🚫 **What This Middleware Does NOT Do**

| Feature | Status | Why Not? |
|---------|--------|----------|
| **Authentication** | ❌ Not here | Uses FastAPI dependencies (`Depends(get_current_user)`) |
| **Rate Limiting** | ❌ Not needed | Single-user local system |
| **Request Validation** | ❌ Not here | Pydantic schemas handle this |
| **CORS Handling** | ❌ Separate | Uses built-in `CORSMiddleware` |
| **Response Compression** | ❌ Not needed | Modern browsers + small responses |

---

## 🔮 **Potential Future Enhancements**

If the project grows, consider adding:

### **Metrics Collection**
```python
# Prometheus metrics
REQUEST_COUNT = Counter('http_requests_total', 'Total requests')
REQUEST_DURATION = Histogram('http_request_duration_seconds', 'Request duration')
```

### **Request/Response Body Logging** (for debugging only)
```python
# WARNING: Can expose sensitive data, use carefully
if settings.debug:
    body = await request.body()
    log_data['request_body'] = body.decode()
```

### **Custom Headers**
```python
# Add server info to response
response.headers["X-Server-Version"] = "1.0.0"
response.headers["X-Process-Time"] = str(process_time)
```

---

## 📚 **Related Documentation**

- [Core Logging](../core/README.md#logging_configpy) - Logging configuration and loggers
- [FastAPI Middleware](https://fastapi.tiangolo.com/advanced/middleware/) - Official middleware docs
- [Starlette Middleware](https://www.starlette.io/middleware/) - Base middleware framework

---

## 💡 **Best Practices**

1. **Keep middleware thin** - Heavy logic belongs in route handlers or services
2. **Don't block requests** - Async operations only, never use `time.sleep()`
3. **Handle exceptions** - Always catch and log errors without breaking the request flow
4. **Sanitize sensitive data** - Never log passwords, tokens, or API keys in plain text
5. **Monitor performance** - Track middleware overhead with metrics

---

## 🎓 **Implementation Details**

**Class:** `RequestLoggingMiddleware`  
**Base:** `BaseHTTPMiddleware` (Starlette)  
**Method:** `async def dispatch(request, call_next)`

**Flow:**
1. Generate `request_id`
2. Extract client info (IP, user agent)
3. Sanitize headers
4. Log request start
5. Call `await call_next(request)` → route handler
6. Calculate processing time
7. Log response
8. Add `X-Request-ID` header
9. Return response

**Exception Handling:**
- Catches all exceptions during request processing
- Logs error with full context
- Re-raises exception (doesn't suppress errors)
