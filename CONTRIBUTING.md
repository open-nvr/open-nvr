# Contributing to OpenNVR

Thank you for your interest in contributing to OpenNVR! We welcome contributions from developers, security researchers, AI/ML engineers, and documentation writers. This guide will help you get started.

## 🌟 Ways to Contribute

### 1. 🐛 Bug Reports & Feature Requests
- Check [existing issues](https://github.com/[your-org]/opennvr/issues) before creating new ones
- Use issue templates when available
- Provide detailed reproduction steps for bugs
- Include system information (OS, versions, logs)

### 2. 💻 Code Contributions
- Fix bugs or implement new features
- Optimize performance bottlenecks
- Add tests to improve coverage
- Refactor code for better maintainability

### 3. 🤖 AI Model Contributions
- Submit new model handlers for the AI Adapter registry
- Optimize existing models for better accuracy/performance
- Add support for new AI frameworks or cloud providers
- Create benchmarks and evaluation scripts

### 4. 📖 Documentation
- Improve README and guides
- Write tutorials or blog posts
- Create video demonstrations
- Translate documentation to other languages

### 5. 🔍 Security Research
- Report vulnerabilities responsibly (see [SECURITY.md](SECURITY.md))
- Contribute security hardening improvements
- Review code for security issues
- Add security test cases

---

## 🚀 Getting Started

### 1. Fork and Clone

```bash
# Fork repository on GitHub, then:
git clone https://github.com/YOUR_USERNAME/opennvr.git
cd opennvr
git remote add upstream https://github.com/[your-org]/opennvr.git
```

### 2. Set Up Development Environment

#### Backend (Python)
```bash
cd server
uv venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
uv pip install -r requirements.txt
uv pip install -r requirements-dev.txt  # Testing & linting tools

# Install pre-commit hooks (optional but recommended)
pre-commit install
```

#### Frontend (TypeScript/React)
```bash
cd app
npm install
```

#### AI Adapters
```bash
cd AI-adapters/AIAdapters
uv pip install -r requirements.txt
```

### 3. Create a Feature Branch

```bash
git checkout -b feature/your-feature-name
# or
git checkout -b bugfix/issue-123
```

---

## 📝 Code Guidelines

### Python Code Style

We follow **PEP 8** with the following specifics:

- **Type hints** are required for function signatures
- **Docstrings** required for all public functions (Google style)
- **Line length**: 100 characters max
- **Import order**: standard library → third-party → local (use `isort`)

**Example**:
```python
from typing import Optional

def get_camera_by_id(camera_id: int, db: Session) -> Optional[Camera]:
    """
    Retrieve a camera by its ID.

    Args:
        camera_id: The unique identifier of the camera
        db: Database session

    Returns:
        Camera object if found, None otherwise
    
    Raises:
        DatabaseError: If database query fails
    """
    return db.query(Camera).filter(Camera.id == camera_id).first()
```

**Linting & Formatting**:
```bash
# Format code
black server/
isort server/

# Check linting
flake8 server/
mypy server/
```

### TypeScript Code Style

- **ESLint** configured in `app/eslint.config.js`
- **Type safety**: No `any` types unless absolutely necessary
- **Component structure**: Functional components with hooks
- **File naming**: PascalCase for components, camelCase for utilities

**Example**:
```typescript
interface CameraProps {
  cameraId: number;
  onStreamStart?: () => void;
}

export const CameraLiveView: React.FC<CameraProps> = ({ cameraId, onStreamStart }) => {
  const [isStreaming, setIsStreaming] = useState<boolean>(false);
  
  // Component logic...
};
```

**Linting**:
```bash
cd app
npm run lint
npm run type-check
```

### Commit Messages

We use **Conventional Commits** format:

```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types**:
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, no logic change)
- `refactor`: Code refactoring
- `perf`: Performance improvements
- `test`: Adding or updating tests
- `chore`: Maintenance tasks

**Examples**:
```bash
feat(ai): add YOLOv11 model handler with ByteTrack
fix(auth): prevent session timeout during active streaming
docs(readme): update installation instructions for Windows
refactor(api): simplify camera configuration validation
```

---

## 🤖 Contributing AI Models

### AI Adapter Architecture

OpenNVR uses a modular **handler pattern** for AI models. Each model handler:

1. Inherits from `BaseModelHandler`
2. Implements `get_supported_tasks()` and `infer()` methods
3. Registered in `adapter/config.py`

### Step-by-Step Model Contribution

#### 1. Create Model Handler

```python
# AI-adapters/AIAdapters/adapter/models/your_model_handler.py
from .base_handler import BaseModelHandler
from typing import Dict, List, Any
import onnxruntime as ort

class YourModelHandler(BaseModelHandler):
    """Handler for [Model Name] - [Brief description]."""
    
    def __init__(self, model_path: str):
        """
        Initialize the model handler.
        
        Args:
            model_path: Path to model weights
        """
        super().__init__(model_path)
        self.session = ort.InferenceSession(model_path)
        # Load model-specific configurations
        
    def get_supported_tasks(self) -> List[str]:
        """Return list of tasks this model can perform."""
        return ["task_name_1", "task_name_2"]
    
    def infer(self, task: str, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Run inference on input data.
        
        Args:
            task: Task identifier
            input_data: Input data dictionary with 'frame' key
            
        Returns:
            Inference results matching the task's response schema
        """
        self.validate_task(task)
        
        # Load and preprocess image
        frame = load_image_from_uri(input_data["frame"]["uri"])
        
        # Run inference
        results = self._run_model(frame)
        
        # Format response
        return self._format_results(results, task)
```

#### 2. Register in Config

```python
# AI-adapters/AIAdapters/adapter/config.py

MODEL_CONFIGS = {
    # ... existing models ...
    "your_model": {
        "path": "model_weights/your_model.onnx",
        "handler_class": "YourModelHandler",
        "description": "Brief description of your model"
    }
}

ENABLED_TASKS = {
    # ... existing tasks ...
    "task_name_1": True,
    "task_name_2": True
}
```

#### 3. Add Response Schema

```python
# AI-adapters/AIAdapters/adapter/response_schemas.py

RESPONSE_SCHEMAS = {
    # ... existing schemas ...
    "task_name_1": {
        "field_name": {
            "type": "string",
            "description": "Description of the field",
            "example": "example_value"
        }
    }
}
```

#### 4. Create Tests

```python
# AI-adapters/AIAdapters/tests/test_your_model.py
import pytest
from adapter.models.your_model_handler import YourModelHandler

def test_your_model_inference():
    handler = YourModelHandler("model_weights/your_model.onnx")
    
    input_data = {
        "frame": {"uri": "kavach://frames/camera_0/test.jpg"}
    }
    
    result = handler.infer("task_name_1", input_data)
    
    assert "expected_field" in result
    assert result["expected_field"] is not None
```

#### 5. Document Performance

Create a benchmark file:

```markdown
# Model: YourModel
- **Task**: task_name_1
- **Framework**: ONNX Runtime / PyTorch / TensorFlow
- **Input Size**: 640x640
- **Hardware**: Intel i7-10700 (CPU only)
- **Avg Latency**: ~500ms
- **Accuracy**: mAP@0.5: 0.85
- **Model Size**: 25MB
```

#### 6. Submit Pull Request

Include:
- ✅ Handler implementation
- ✅ Config registration
- ✅ Tests with >80% coverage
- ✅ Performance benchmarks
- ✅ Usage documentation
- ✅ Model weights (if <100MB) or download script

---

## 🧪 Testing

### Backend Tests

```bash
cd server
pytest                       # Run all tests
pytest -v                    # Verbose output
pytest tests/test_auth.py    # Specific test file
pytest --cov=.               # With coverage report
```

### Frontend Tests

```bash
cd app
npm test                     # Run all tests
npm test -- --coverage       # With coverage
```

### AI Adapter Tests

```bash
cd AI-adapters/AIAdapters
pytest tests/
```

### Integration Tests

```bash
# Start all services first, then:
pytest integration_tests/
```

---

## 📋 Pull Request Process

1. **Update Documentation**: If you changed behavior, update relevant docs
2. **Add Tests**: New features require tests
3. **Run Linters**: Ensure code passes all checks
4. **Update Changelog**: Add entry to CHANGELOG.md (if exists)
5. **Create PR**: Use the pull request template
6. **Respond to Reviews**: Address feedback promptly

### PR Checklist

- [ ] Code follows project style guidelines
- [ ] Self-review completed
- [ ] Comments added for complex logic
- [ ] Documentation updated
- [ ] Tests added/updated
- [ ] All tests passing
- [ ] No new warnings introduced
- [ ] PR description clearly explains changes

---

## 🏷️ Issue Labels

| Label | Description |
|-------|-------------|
| `bug` | Something isn't working |
| `enhancement` | New feature or request |
| `documentation` | Documentation improvements |
| `good first issue` | Good for newcomers |
| `help wanted` | Extra attention needed |
| `security` | Security-related issue |
| `ai-model` | AI model contribution |
| `performance` | Performance optimization |

---

## 🤝 Code of Conduct

### Our Pledge

We are committed to providing a welcoming and inclusive environment for all contributors.

### Standards

**Positive behavior**:
- Using welcoming and inclusive language
- Respecting differing viewpoints
- Accepting constructive criticism gracefully
- Focusing on what's best for the community

**Unacceptable behavior**:
- Harassment or discriminatory language
- Trolling or inflammatory comments
- Public or private harassment
- Publishing others' private information

### Enforcement

Violations can be reported to [contact email]. All complaints will be reviewed and investigated.

---

## 📞 Getting Help

- **Documentation**: Check [docs/](docs/) folder
- **Issues**: Search existing issues or create new one
- **Discussions**: Use GitHub Discussions for questions
- **Discord**: [Join our community](https://discord.gg/opennvr) *(if applicable)*

---

## 🎉 Recognition

Contributors will be recognized in:
- README.md contributors section
- Release notes for significant contributions
- Community showcase for notable features

Thank you for helping make OpenNVR better! 🚀

---

**Questions?** Feel free to ask in [GitHub Discussions](https://github.com/[your-org]/opennvr/discussions) or open an issue.
