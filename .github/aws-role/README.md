# aws role

```bash
export NAME="lambda-gurumi-ai-bot"
```

## create role

```bash
export DESCRIPTION="${NAME} role"

aws iam create-role --role-name "${NAME}" --description "${DESCRIPTION}" --assume-role-policy-document file://trust-policy.json | jq .

aws iam get-role --role-name "${NAME}" | jq .
```

## create policy

```bash
export DESCRIPTION="${NAME} policy"

aws iam create-policy --policy-name "${NAME}" --policy-document file://role-policy.json | jq .

export ACCOUNT_ID=$(aws sts get-caller-identity | jq .Account -r)
export POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${NAME}"

aws iam get-policy --policy-arn "${POLICY_ARN}" | jq .

aws iam create-policy-version --policy-arn "${POLICY_ARN}" --policy-document file://role-policy.json --set-as-default | jq .
```

## attach role policy

```bash
aws iam attach-role-policy --role-name "${NAME}" --policy-arn "${POLICY_ARN}"
# aws iam attach-role-policy --role-name "${NAME}" --policy-arn "arn:aws:iam::aws:policy/PowerUserAccess"
# aws iam attach-role-policy --role-name "${NAME}" --policy-arn "arn:aws:iam::aws:policy/AdministratorAccess"
```

## add role-assume

```yaml

      - name: configure aws credentials
        uses: aws-actions/configure-aws-credentials@v1.7.0
        with:
          role-to-assume: "arn:aws:iam::968005369378:role/lambda-gurumi-ai-bot"
          role-session-name: github-actions-ci-bot
          aws-region: ${{ env.AWS_REGION }}

      - name: Sts GetCallerIdentity
        run: |
          aws sts get-caller-identity

```
