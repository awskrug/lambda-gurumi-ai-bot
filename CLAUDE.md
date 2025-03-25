# CLAUDE.md - Lambda Gurumi AI Bot

## Basic Guidelines
- Focus on solving the specific problem - avoid unnecessary complexity or scope creep.
- Use standard libraries and documented patterns first before creating custom solutions.
- Write clean, well-structured code with meaningful names and clear organization.
- Handle errors and edge cases properly to ensure code robustness.
- Include helpful comments for complex logic while keeping code self-documenting.

## Build/Deploy Commands
```bash
npm install                        # Install JavaScript dependencies
pip install -r requirements.txt    # Install Python dependencies
sls deploy --region us-east-1      # Deploy to AWS Lambda
```

## Code Style Guidelines
- **Python Version**: 3.9
- **Naming**: Functions/variables use `snake_case`, constants use `UPPER_SNAKE_CASE`
- **Imports**: Standard library first, third-party modules second, local modules last
- **Indentation**: 4 spaces
- **Type Hints**: Use when appropriate for function signatures
- **Environment Variables**: Use `os.environ.get("VAR_NAME", "default_value")`
- **Error Handling**: Use try/except with specific exceptions:
  ```python
  try:
      # Code that may raise exception
  except SpecificException as e:
      print(f"function_name: Error: {e}")
      raise
  ```
- **Documentation**: Use docstrings for functions describing purpose and parameters
- **Line Length**: Keep reasonable (no strict limit enforced)

## Tech Stack
- **AWS Lambda** with Serverless Framework
- **Amazon Bedrock** for AI capabilities (Claude 3, Stable Diffusion)
- **DynamoDB** for context storage
- **Slack** and **Kakao** messaging integration
