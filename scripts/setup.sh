#!/bin/bash

echo "Setting up OpenNVR Surveillance System API Virtual Environment..."
echo

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed or not in PATH"
    echo "Please install Python 3 from https://python.org"
    exit 1
fi

echo "Python 3 found, creating virtual environment..."
python3 setup_venv.py

if [ $? -ne 0 ]; then
    echo
    echo "Setup failed. Please check the error messages above."
    exit 1
fi

echo
echo "Setup completed successfully!"
echo
echo "To activate the virtual environment, run:"
echo "  source venv/bin/activate"
echo
echo "Or use the convenience script:"
echo "  ./activate_venv.sh"
echo


