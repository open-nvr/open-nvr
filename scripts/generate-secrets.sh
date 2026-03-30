#!/bin/bash
# ============================================
# OpenNVR - Secrets Generator
# ============================================
# This script generates all required secrets for .env file
# Run this script and copy the output to your .env file
# ============================================

echo "============================================"
echo "OpenNVR - SECURITY SECRETS GENERATOR"
echo "============================================"
echo ""
echo "Generating secure random secrets..."
echo ""

# Check if openssl is available
if ! command -v openssl &> /dev/null; then
    echo "ERROR: openssl not found!"
    echo "Please install openssl first:"
    echo "  - Ubuntu/Debian: sudo apt-get install openssl"
    echo "  - macOS: brew install openssl"
    echo "  - Windows: choco install openssl"
    exit 1
fi

# Check if python is available for Fernet key
if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo "WARNING: Python not found! Cannot generate CREDENTIAL_ENCRYPTION_KEY"
    echo "Please install Python 3 and cryptography package"
    PYTHON_AVAILABLE=false
else
    PYTHON_AVAILABLE=true
    # Determine python command
    if command -v python3 &> /dev/null; then
        PYTHON_CMD=python3
    else
        PYTHON_CMD=python
    fi
fi

echo "============================================"
echo "COPY THESE VALUES TO YOUR .env FILE"
echo "============================================"
echo ""

# Generate database password
DB_PASSWORD=$(openssl rand -base64 32 | tr -d "=+/" | cut -c1-32)
echo "# Database Configuration"
echo "POSTGRES_PASSWORD=$DB_PASSWORD"
echo "DATABASE_URL=postgresql://opennvr_user:$DB_PASSWORD@db:5432/opennvr_db"
echo ""

# Generate JWT secret key
SECRET_KEY=$(openssl rand -hex 32)
echo "# JWT Secret Key"
echo "SECRET_KEY=$SECRET_KEY"
echo ""

# Generate credential encryption key
if [ "$PYTHON_AVAILABLE" = true ]; then
    CREDENTIAL_KEY=$($PYTHON_CMD -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null)
    if [ $? -eq 0 ]; then
        echo "# Credential Encryption Key"
        echo "CREDENTIAL_ENCRYPTION_KEY=$CREDENTIAL_KEY"
    else
        echo "# Credential Encryption Key (FAILED - install cryptography: uv pip install cryptography)"
        echo "CREDENTIAL_ENCRYPTION_KEY=GENERATE_MANUALLY"
    fi
else
    echo "# Credential Encryption Key (Python not found)"
    echo "CREDENTIAL_ENCRYPTION_KEY=GENERATE_MANUALLY"
fi
echo ""

# Generate internal API key
INTERNAL_API_KEY=$(openssl rand -base64 32)
echo "# Internal API Key"
echo "INTERNAL_API_KEY=$INTERNAL_API_KEY"
echo ""

# Generate MediaMTX secret
MEDIAMTX_SECRET=$(openssl rand -hex 16)
echo "# MediaMTX Secret"
echo "MEDIAMTX_SECRET=$MEDIAMTX_SECRET"
echo ""

echo "============================================"
echo "GENERATION COMPLETE"
echo "============================================"
echo ""
echo "⚠️  IMPORTANT:"
echo "  1. Copy ALL the values above to your .env file"
echo "  2. Keep these secrets SECURE and PRIVATE"
echo "  3. NEVER commit .env file to version control"
echo "  4. Store a backup of these secrets in a secure location"
echo ""
echo "✅ After copying to .env, you can run:"
echo "   docker compose up -d"
echo ""
