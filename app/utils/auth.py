"""
JWT Authentication Middleware for S12 Service

Implements JWKS-based JWT verification according to the specification in docs/auth/VERIFY.md.

Features:
- Fetches JWKS from Identity Service at startup and on TTL schedule
- Verifies RS256 JWT signatures
- Validates standard claims (exp, iat, sub, full_name, email, permissions)
- Provides Flask decorator for protecting endpoints
"""

import os
import jwt
import threading
import time
import requests
from functools import wraps
from typing import Optional, Callable, Any
from flask import request, g
from flask_restx import abort
from datetime import datetime, timezone


class JWKSManager:
    """
    Manages JWKS (JSON Web Key Set) fetching and caching.
    
    Fetches public keys from Identity Service and caches them
    for JWT verification. Implements TTL-based refresh.
    """
    
    def __init__(self):
        self._keys: dict[str, dict] = {}  # kid -> key info
        self._lock = threading.Lock()
        self._last_refresh: Optional[float] = None
        self._refresh_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        
        # Configuration from environment
        self.identity_service_url = os.getenv("IDENTITY_SERVICE_URL", "http://localhost:8000")
        self.jwks_ttl_minutes = int(os.getenv("JWKS_TTL_IN_MINUTES", "10"))
        
        # Initial fetch
        self._refresh_jwks()
        
        # Start background refresh thread
        self._start_refresh_thread()
    
    def _get_jwks_url(self) -> str:
        """Get the JWKS endpoint URL."""
        return f"{self.identity_service_url}/.well-known/jwks.json"
    
    def _refresh_jwks(self):
        """
        Fetch JWKS from Identity Service.
        
        On failure during scheduled refresh, continues using cached keys.
        """
        try:
            response = requests.get(self._get_jwks_url(), timeout=10)
            response.raise_for_status()
            
            jwks_data = response.json()
            
            # Process keys
            new_keys = {}
            
            # Handle both single key and array of keys format
            keys_list = jwks_data if isinstance(jwks_data, list) else [jwks_data]
            
            for key_data in keys_list:
                kid = key_data.get("kid")
                if not kid:
                    continue
                    
                # Validate key type and algorithm
                kty = key_data.get("kty")
                alg = key_data.get("alg")
                use = key_data.get("use")
                
                if kty != "RSA" or alg != "RS256" or use != "sig":
                    continue
                
                public_key_pem = key_data.get("public_key")
                if not public_key_pem:
                    continue
                
                new_keys[kid] = {
                    "kid": kid,
                    "kty": kty,
                    "alg": alg,
                    "public_key": public_key_pem,
                    "use": use
                }
            
            with self._lock:
                self._keys = new_keys
                self._last_refresh = time.time()
                
            print(f"[JWKSManager] Successfully refreshed JWKS. {len(new_keys)} keys loaded.")
            
        except Exception as e:
            # Log warning but continue with cached keys
            print(f"[JWKSManager] Warning: Failed to refresh JWKS: {e}")
            # Only raise on initial fetch (no cached keys)
            with self._lock:
                if not self._keys:
                    print("[JWKSManager] No cached keys available. JWT verification will fail.")
    
    def _start_refresh_thread(self):
        """Start background thread for TTL-based JWKS refresh."""
        def _refresh_loop():
            while not self._stop_event.is_set():
                # Wait for TTL
                self._stop_event.wait(timeout=self.jwks_ttl_minutes * 60)
                if self._stop_event.is_set():
                    break
                # Refresh JWKS
                self._refresh_jwks()
        
        self._refresh_thread = threading.Thread(target=_refresh_loop, daemon=True)
        self._refresh_thread.start()
    
    def stop(self):
        """Stop the background refresh thread."""
        self._stop_event.set()
        if self._refresh_thread:
            self._refresh_thread.join(timeout=5)
    
    def get_public_key(self, kid: str) -> Optional[str]:
        """
        Get public key by kid.
        
        IMPORTANT: Does NOT refresh JWKS on cache miss (DoS prevention).
        
        Args:
            kid: Key ID from JWT header
            
        Returns:
            PEM-encoded public key or None if not found
        """
        with self._lock:
            key_info = self._keys.get(kid)
            return key_info.get("public_key") if key_info else None


# Global JWKS manager instance
_jwks_manager: Optional[JWKSManager] = None
_jwks_manager_lock = threading.Lock()


def get_jwks_manager() -> JWKSManager:
    """Get or create the global JWKS manager instance."""
    global _jwks_manager
    with _jwks_manager_lock:
        if _jwks_manager is None:
            _jwks_manager = JWKSManager()
        return _jwks_manager


def verify_jwt_token(token: str) -> dict:
    """
    Verify a JWT token and return its claims.
    
    Implements strict verification according to VERIFY.md:
    1. Parse JWT header, extract alg and kid
    2. Resolve public key from cached JWKS
    3. Verify signature with RS256
    4. Validate claims (exp, iat, sub, full_name, email)
    
    Args:
        token: The JWT token string
        
    Returns:
        dict containing validated JWT claims
        
    Raises:
        ValueError with appropriate message on any verification failure
    """
    try:
        # Step 1: Parse JWT header
        unverified_header = jwt.get_unverified_header(token)
        
        alg = unverified_header.get("alg")
        kid = unverified_header.get("kid")
        
        if alg != "RS256":
            raise ValueError("Invalid algorithm. Only RS256 is supported.")
        
        if not kid:
            raise ValueError("Missing kid in JWT header.")
        
        # Step 2: Resolve public key
        jwks_manager = get_jwks_manager()
        public_key_pem = jwks_manager.get_public_key(kid)
        
        if not public_key_pem:
            # DO NOT refresh JWKS - reject with 401
            raise ValueError(f"Unknown key ID: {kid}")
        
        # Step 3: Verify signature and decode
        claims = jwt.decode(
            token,
            public_key_pem,
            algorithms=["RS256"],
            options={
                "require": ["exp", "iat", "sub"],
                "verify_exp": True,
                "verify_iat": True,
            }
        )
        
        # Step 4: Validate required claims
        if not claims.get("sub"):
            raise ValueError("Missing required claim: sub")
        
        # full_name and email are required but may be empty strings
        if "full_name" not in claims:
            raise ValueError("Missing required claim: full_name")
        
        if "email" not in claims:
            raise ValueError("Missing required claim: email")
        
        # Ensure permissions is a list (default to empty list if not present)
        permissions = claims.get("permissions", [])
        if not isinstance(permissions, list):
            claims["permissions"] = []
        
        return claims
        
    except jwt.ExpiredSignatureError:
        raise ValueError("Token has expired")
    except jwt.PyJWTError as e:
        raise ValueError(f"Invalid token: {str(e)}")


def jwt_required(f: Callable) -> Callable:
    """
    Flask decorator that requires a valid JWT token.
    
    Extracts JWT from Authorization header (Bearer token),
    verifies it, and stores claims in Flask's g object.
    
    Usage:
        @api.route("/protected")
        class ProtectedResource(Resource):
            @jwt_required
            def get(self):
                user_id = g.jwt_claims["sub"]
                return {"user_id": user_id}
    """
    @wraps(f)
    def decorated(*args: Any, **kwargs: Any) -> Any:
        # Get Authorization header
        auth_header = request.headers.get("Authorization")
        
        if not auth_header:
            abort(401, "Token xác thực không hợp lệ hoặc đã hết hạn")
        
        # Parse Bearer token
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            abort(401, "Token xác thực không hợp lệ hoặc đã hết hạn")
        
        token = parts[1]
        
        try:
            claims = verify_jwt_token(token)
            g.jwt_claims = claims
            g.user_id = claims.get("sub")
            g.user_email = claims.get("email")
            g.user_full_name = claims.get("full_name")
            g.user_permissions = claims.get("permissions", [])
        except ValueError as e:
            # Log for audit purposes
            print(f"[JWT] Verification failed: {e}")
            abort(401, "Token xác thực không hợp lệ hoặc đã hết hạn")
        
        return f(*args, **kwargs)
    
    return decorated


def permission_required(*required_permissions: str) -> Callable:
    """
    Flask decorator that requires specific permissions.
    
    Must be used after @jwt_required decorator.
    
    Usage:
        @api.route("/admin")
        class AdminResource(Resource):
            @jwt_required
            @permission_required("admin:write")
            def post(self):
                return {"message": "Admin action performed"}
    """
    def decorator(f: Callable) -> Callable:
        @wraps(f)
        def decorated(*args: Any, **kwargs: Any) -> Any:
            user_permissions = getattr(g, 'user_permissions', [])
            
            for perm in required_permissions:
                if perm not in user_permissions:
                    abort(403, "Người dùng không có quyền thực hiện hành động này")
            
            return f(*args, **kwargs)
        return decorated
    return decorator
