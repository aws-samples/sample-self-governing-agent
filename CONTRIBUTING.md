# Contributing

Thank you for your interest in contributing to this project.

## Security

If you discover a potential security issue, please do NOT create a public issue. Instead, send your report privately.

## Reporting Bugs/Feature Requests

Please use the GitHub Issues tab to report bugs or suggest features.

## Contributing via Pull Requests

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes following existing Python conventions (type hints, small
   single-responsibility functions)
4. Run the offline test suites — they need no AWS credentials:
   ```
   python -m tests.test_cedar_authorization
   python -m tests.test_interceptor
   python -m tests.test_token_claims
   python -m tests.test_feedback_convergence
   ```
5. Submit a pull request with a clear description

## Code Style

- Python 3.12; type hints on public functions
- Keep the interceptor path minimal (it runs on every tool call)

## License

By contributing, you agree that your contributions will be licensed under MIT-0.
