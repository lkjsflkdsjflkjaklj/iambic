from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Callable, List, Optional, Union

from pydantic import Field, validator

from iambic.core.context import ctx
from iambic.core.iambic_enum import Command, IambicManaged
from iambic.core.logger import log
from iambic.core.models import (
    AccessModelMixin,
    AccountChangeDetails,
    BaseModel,
    ProposedChange,
    ProposedChangeType,
    TemplateChangeDetails,
)
from iambic.core.utils import aio_wrapper, evaluate_on_provider, plugin_apply_wrapper
from iambic.plugins.v0_1_0.aws.iam.policy.models import PolicyStatement
from iambic.plugins.v0_1_0.aws.identity_center.permission_set.utils import (
    apply_account_assignments,
    apply_permission_set_aws_managed_policies,
    apply_permission_set_customer_managed_policies,
    apply_permission_set_inline_policy,
    apply_permission_set_permission_boundary,
    apply_permission_set_tags,
    delete_permission_set,
    enrich_permission_set_details,
    get_permission_set_users_and_groups_as_access_rules,
)
from iambic.plugins.v0_1_0.aws.models import (
    AccessModel,
    AWSAccount,
    AWSTemplate,
    Description,
    ExpiryModel,
    Tag,
)
from iambic.plugins.v0_1_0.aws.utils import boto_crud_call, remove_expired_resources

if TYPE_CHECKING:
    from iambic.plugins.v0_1_0.aws.iambic_plugin import AWSConfig

AWS_IDENTITY_CENTER_PERMISSION_SET_TEMPLATE_TYPE = (
    "NOQ::AWS::IdentityCenter::PermissionSet"
)

# TODO: Add true support for defining multiple orgs with IdentityCenter rules


class PermissionSetAccess(AccessModel, ExpiryModel):
    users: list[str] = Field(
        [],
        description="List of users who can access the role",
    )
    groups: list[str] = Field(
        [],
        description="List of groups. Users in one or more of the groups can access the role",
    )

    @property
    def resource_type(self):
        return "aws:identity_center:permission_set_access"

    @property
    def resource_id(self):
        return ""


class AWSIdentityCenterInstance(BaseModel):
    arn: str
    region: str
    access_portal_url: str
    identity_store_id: str


class CustomerManagedPolicyReference(BaseModel, ExpiryModel):
    path: str
    name: str

    @property
    def resource_type(self) -> str:
        return "aws:iam:managed_policy"

    @property
    def resource_id(self) -> str:
        return f"{self.path}{self.name}"


class ManagedPolicyArn(BaseModel, ExpiryModel):
    arn: str

    @property
    def resource_type(self) -> str:
        return "aws:iam:managed_policy"

    @property
    def resource_id(self) -> str:
        return self.arn


class PermissionBoundary(BaseModel, ExpiryModel):
    customer_managed_policy_reference: Optional[CustomerManagedPolicyReference]
    policy_arn: Optional[str]

    @property
    def resource_type(self):
        return "aws:identity_center:permission_boundary"

    @property
    def resource_id(self):
        return self.customer_managed_policy_reference.name or self.policy_arn


class SessionDuration(BaseModel):
    session_duration: str


class InlinePolicy(BaseModel, ExpiryModel):
    version: Optional[str] = None
    statement: Optional[List[PolicyStatement]] = Field(
        None,
        description="List of policy statements",
    )

    @property
    def resource_type(self) -> str:
        return "aws:identity_center:inline_policy"

    @property
    def resource_id(self) -> str:
        return str(self.statement)


class PermissionSetProperties(BaseModel):
    name: str
    description: Optional[Union[str, list[Description]]] = Field(
        None,
        description="Description of the permission set",
    )
    relay_state: Optional[str] = None
    session_duration: Optional[Union[str, list[SessionDuration]]] = None
    permissions_boundary: Optional[PermissionBoundary] = None
    inline_policy: Optional[InlinePolicy] = None
    customer_managed_policy_references: Optional[
        list[CustomerManagedPolicyReference]
    ] = []
    managed_policies: Optional[list[ManagedPolicyArn]] = []
    tags: Optional[list[Tag]] = []

    @property
    def resource_type(self) -> str:
        return "aws:identity_center:permission_set"

    @property
    def resource_id(self) -> str:
        return self.name

    @classmethod
    def sort_func(cls, attribute_name: str) -> Callable:
        def _sort_func(obj):
            return f"{getattr(obj, attribute_name)}!{obj.access_model_sort_weight()}"

        return _sort_func

    @validator("description")
    def validate_description(cls, v: Union[str, list[Description]]):
        # validation portion
        if isinstance(v, str) and not (1 <= len(v) <= 700):
            raise ValueError(
                f"description must be between 1 and 700 characters: given {v}"
            )
        if isinstance(v, list):
            for description in v:
                description: Description
                if not (1 <= len(description.description) <= 700):
                    raise ValueError(
                        f"description must be between 1 and 700 characters: given {description.description}"
                    )

        # sorting portion
        if not isinstance(v, list):
            return v

        return sorted(v, key=lambda d: d.access_model_sort_weight())

    @validator("managed_policies")
    def sort_managed_policy_refs(cls, v: list[ManagedPolicyArn]):
        sorted_v = sorted(v, key=lambda o: o.resource_id)
        return sorted_v

    @validator("customer_managed_policy_references")
    def sort_customer_managed_policy_references(
        cls, v: list[CustomerManagedPolicyReference]
    ):
        sorted_v = sorted(v, key=lambda o: o.resource_id)
        return sorted_v

    @validator("tags")
    def sort_tags(cls, v: list[Tag]):
        sorted_v = sorted(v, key=cls.sort_func("key"))
        return sorted_v


class AwsIdentityCenterPermissionSetTemplate(
    AccessModelMixin, AWSTemplate, ExpiryModel
):
    template_type: str = AWS_IDENTITY_CENTER_PERMISSION_SET_TEMPLATE_TYPE
    owner: Optional[str] = Field(None, description="Owner of the permission set")
    properties: PermissionSetProperties
    access_rules: Optional[list[PermissionSetAccess]] = []
    included_orgs: list[str] = Field(
        ["*"],
        description=(
            "A list of AWS organization ids this statement applies to. "
            "Org ids can be represented as a regex and string"
        ),
    )
    excluded_orgs: Optional[list[str]] = Field(
        [],
        description=(
            "A list of AWS organization ids this statement explicitly does not apply to. "
            "Org ids can be represented as a regex and string"
        ),
    )

    @classmethod
    def iambic_specific_knowledge(cls) -> set[str]:
        return {"access_rules"}

    @validator("access_rules")
    def sort_access_rules(cls, v: list[PermissionSetAccess]):
        sorted_v = sorted(v, key=lambda o: o.access_model_sort_weight())
        return sorted_v

    async def _access_rules_for_account(  # noqa: C901
        self,
        aws_account: AWSAccount,
        account_id: str,
        account_name: str,
        reverse_user_map: dict[str, str],
        reverse_group_map: dict[str, str],
    ) -> dict:
        """
        Builds a list of access rules for a given account.

        return: {
                "account_id": account_id,
                "user": list(user_ids),
                "group": list(group_ids),
            }
        """

        user_assignments = set()
        group_assignments = set()

        for rule in self.access_rules:
            rule_hit = None

            if rule.deleted:
                continue

            # If the account's org is excluded or not included, skip
            if aws_account.org_id in rule.excluded_orgs:
                continue
            elif "*" not in rule.included_orgs and not any(
                org_id == aws_account.org_id for org_id in rule.included_orgs
            ):
                continue

            if account_name:
                account_reprs = [account_id, account_name.lower()]
            else:
                account_reprs = [account_id]

            # Check against the ways an account can be represented
            # Compare against excluded accounts
            # If it hits on an excluded account rule, skip
            for account_repr in account_reprs:
                for resource_account in rule.excluded_accounts:
                    try:
                        is_hit = await aio_wrapper(
                            re.match, resource_account.lower(), account_repr
                        )
                    except Exception:
                        # Catch accounts with a name that is not a valid regex
                        is_hit = bool(resource_account.lower() == account_repr)

                    if is_hit:
                        rule_hit = False
                        break

                if rule_hit is False:
                    break

            if rule_hit is False:
                continue

            if any(
                resource_account == "*" for resource_account in rule.included_accounts
            ):
                rule_hit = True
            else:
                # Check against the ways an account can be represented
                # Compare against included accounts
                # If it hits on an included account rule
                #   Stop the check and add the users and groups on the rule
                for account_repr in account_reprs:
                    for resource_account in rule.included_accounts:
                        try:
                            is_hit = await aio_wrapper(
                                re.match, resource_account.lower(), account_repr
                            )
                        except Exception:
                            # Catch accounts with a name that is not a valid regex
                            is_hit = bool(resource_account.lower() == account_repr)

                        if is_hit:
                            rule_hit = True
                            break

                    if rule_hit:
                        break

            if rule_hit:
                for rule_user in rule.users:
                    if rule_user == "*":
                        user_assignments.update(reverse_user_map.values())
                    elif user_hit := reverse_user_map.get(rule_user):
                        user_assignments.add(user_hit)

                for rule_group in rule.groups:
                    if rule_group == "*":
                        group_assignments.update(reverse_group_map.values())
                    elif group_hit := reverse_group_map.get(rule_group):
                        group_assignments.add(group_hit)

                if len(group_assignments) == len(reverse_group_map) and len(
                    user_assignments
                ) == len(reverse_user_map):
                    break

        if user_assignments or group_assignments:
            return {
                "account_id": account_id,
                "user": list(user_assignments),
                "group": list(group_assignments),
            }

    async def _verbose_access_rules(self, aws_account: AWSAccount) -> list[dict]:
        """
        Generates the list of access rules across all accounts for the org.
        Fans out calls to _access_rules_for_account and formats the results.
        """
        response = []
        reverse_user_map = {
            details["UserName"]: user_id
            for user_id, details in aws_account.identity_center_details.user_map.items()
        }
        reverse_group_map = {
            details["DisplayName"]: group_id
            for group_id, details in aws_account.identity_center_details.group_map.items()
        }

        account_assignments = await asyncio.gather(
            *[
                self._access_rules_for_account(
                    aws_account,
                    account_id,
                    account_name,
                    reverse_user_map,
                    reverse_group_map,
                )
                for account_id, account_name in aws_account.identity_center_details.org_account_map.items()
            ]
        )

        for account_assignment in account_assignments:
            if not account_assignment:
                continue

            for assignment_type in ["user", "group"]:
                resource_type = assignment_type.upper()
                for assignment in account_assignment[assignment_type]:
                    a_account_id = account_assignment["account_id"]
                    response.append(
                        {
                            "account_id": a_account_id,
                            "resource_id": assignment,
                            "resource_type": resource_type,
                            "resource_name": aws_account.identity_center_details.get_resource_name(
                                resource_type, assignment
                            ),
                            "account_name": f"{a_account_id} ({aws_account.identity_center_details.org_account_map[a_account_id]})",
                        }
                    )

        return response

    def _is_read_only(self, aws_account: AWSAccount):
        return bool(
            aws_account.iambic_managed == IambicManaged.IMPORT_ONLY
            or self.iambic_managed == IambicManaged.IMPORT_ONLY
        )

    async def _apply_to_account(  # noqa: C901
        self, aws_account: AWSAccount
    ) -> AccountChangeDetails:
        """Apply the permission set to the given AWS account

        :param aws_account:
        :return:
        """

        identity_center_client = await aws_account.get_boto3_client(
            "sso-admin", region_name=aws_account.identity_center_details.region_name
        )
        instance_arn = aws_account.identity_center_details.instance_arn
        permission_set_arn = None
        self = await remove_expired_resources(
            self, self.resource_type, self.resource_id
        )
        template_permission_set = self.apply_resource_dict(aws_account)
        name = template_permission_set["Name"]
        template_account_assignments = await self._verbose_access_rules(aws_account)
        account_change_details = AccountChangeDetails(
            org_id=aws_account.org_id,
            account=str(aws_account),
            resource_id=name,
            resource_type=self.resource_type,
            new_value=dict(
                AccountAssignment=template_account_assignments,
                **template_permission_set,
            ),
            proposed_changes=[],
            exceptions_seen=[],
        )
        log_params = dict(
            resource_type=self.resource_type,
            resource_id=name,
            account=str(aws_account),
            org_id=aws_account.org_id,
        )
        read_only = self._is_read_only(aws_account)

        current_account_assignments = {}
        current_permission_set = (
            aws_account.identity_center_details.permission_set_map.get(name, {})
        )
        if current_permission_set:
            exclude_keys = ["CreatedDate", "PermissionSetArn"]
            permission_set_arn = current_permission_set["PermissionSetArn"]
            current_permission_set = await enrich_permission_set_details(
                identity_center_client, instance_arn, current_permission_set
            )
            if managed_policies := current_permission_set.pop(
                "AttachedManagedPolicies", None
            ):
                current_permission_set["ManagedPolicies"] = managed_policies

            current_account_assignments = (
                await get_permission_set_users_and_groups_as_access_rules(
                    identity_center_client,
                    instance_arn,
                    permission_set_arn,
                    aws_account.identity_center_details.user_map,
                    aws_account.identity_center_details.group_map,
                    aws_account.identity_center_details.org_account_map,
                )
            )
            account_change_details.current_value = {
                k: v for k, v in current_permission_set.items() if k not in exclude_keys
            }  # Create a new dict
            account_change_details.current_value[
                "AccountAssignments"
            ] = current_account_assignments

            if ctx.command == Command.CONFIG_DISCOVERY:
                # Don't overwrite a resource during config discovery
                account_change_details.new_value = {}
                return account_change_details

        deleted = self.get_attribute_val_for_account(aws_account, "deleted", False)
        if isinstance(deleted, list):
            deleted = deleted[0].deleted

        if deleted:
            if current_permission_set:
                account_change_details.new_value = None
                account_change_details.proposed_changes.append(
                    ProposedChange(
                        change_type=ProposedChangeType.DELETE,
                        resource_id=name,
                        resource_type=self.resource_type,
                    )
                )
                log_str = "Active resource found with deleted=false."
                if ctx.execute and not read_only:
                    log_str = f"{log_str} Deleting resource..."
                log.info(log_str, **log_params)

                if ctx.execute:
                    await delete_permission_set(
                        identity_center_client,
                        instance_arn,
                        permission_set_arn,
                        current_permission_set,
                        current_account_assignments,
                        log_params,
                    )
                    self.delete()

            return account_change_details

        permission_set_exists = bool(current_permission_set)
        tasks = []
        try:
            if permission_set_exists:
                tasks.append(
                    apply_permission_set_tags(
                        identity_center_client,
                        instance_arn,
                        permission_set_arn,
                        template_permission_set.get("Tags", []),
                        current_permission_set.get("Tags", []),
                        log_params,
                    )
                )

                supported_update_keys = ["Description", "SessionDuration", "RelayState"]
                update_resource_log_params = {**log_params}
                update_resource_params = {}
                for k in supported_update_keys:
                    if template_permission_set.get(
                        k
                    ) is not None and template_permission_set.get(
                        k
                    ) != current_permission_set.get(
                        k
                    ):
                        update_resource_log_params[k] = dict(
                            old_value=current_permission_set.get(k),
                            new_value=template_permission_set.get(k),
                        )
                        update_resource_params[k] = template_permission_set.get(k)

                if update_resource_params:
                    log_str = "Out of date resource found."
                    proposed_changes = [
                        ProposedChange(
                            change_type=ProposedChangeType.UPDATE,
                            resource_id=name,
                            resource_type=self.resource_type,
                        )
                    ]
                    if ctx.execute:
                        log.info(
                            f"{log_str} Updating resource...",
                            **update_resource_log_params,
                        )
                        apply_awaitable = boto_crud_call(
                            identity_center_client.update_permission_set,
                            InstanceArn=instance_arn,
                            PermissionSetArn=permission_set_arn,
                            **update_resource_params,
                        )
                        tasks.append(
                            plugin_apply_wrapper(apply_awaitable, proposed_changes)
                        )
                    else:
                        log.info(log_str, **update_resource_log_params)
                        account_change_details.proposed_changes.extend(proposed_changes)
            else:
                account_change_details.proposed_changes.append(
                    ProposedChange(
                        change_type=ProposedChangeType.CREATE,
                        resource_id=name,
                        resource_type=self.resource_type,
                    )
                )
                log_str = "New resource found in code."
                if not ctx.execute:
                    log.info(log_str, **log_params)
                    # Exit now because apply functions won't work if resource doesn't exist
                    return account_change_details

                log_str = f"{log_str} Creating resource..."
                log.info(log_str, **log_params)

                permission_set = await boto_crud_call(
                    identity_center_client.create_permission_set,
                    Name=name,
                    InstanceArn=instance_arn,
                    **{
                        param: template_permission_set.get(param)
                        for param in [
                            "Description",
                            "RelayState",
                            "SessionDuration",
                            "Tags",
                        ]
                        if template_permission_set.get(param)
                    },
                )
                permission_set_arn = permission_set["PermissionSet"]["PermissionSetArn"]

        except Exception as e:
            log.error("Unable to generate tasks for resource", error=e, **log_params)
            return account_change_details

        tasks.extend(
            [
                apply_permission_set_aws_managed_policies(
                    identity_center_client,
                    instance_arn,
                    permission_set_arn,
                    [
                        mp["Arn"]
                        for mp in template_permission_set.get("ManagedPolicies", [])
                    ],
                    [
                        mp["Arn"]
                        for mp in current_permission_set.get("ManagedPolicies", [])
                    ],
                    log_params,
                ),
                apply_permission_set_customer_managed_policies(
                    identity_center_client,
                    instance_arn,
                    permission_set_arn,
                    template_permission_set.get("CustomerManagedPolicyReferences", []),
                    current_permission_set.get("CustomerManagedPolicyReferences", []),
                    log_params,
                ),
                apply_permission_set_inline_policy(
                    identity_center_client,
                    instance_arn,
                    permission_set_arn,
                    template_permission_set.get("InlinePolicy", "{}"),
                    current_permission_set.get("InlinePolicy", "{}"),
                    log_params,
                ),
                apply_permission_set_permission_boundary(
                    identity_center_client,
                    instance_arn,
                    permission_set_arn,
                    template_permission_set.get("PermissionsBoundary", {}),
                    current_permission_set.get("PermissionsBoundary", {}),
                    log_params,
                ),
            ]
        )
        try:
            results: list[list[ProposedChange]] = await asyncio.gather(*tasks)

            # apply_account_assignments is a dedicated call due to the request limit on CreateAccountAssignment
            #   per https://docs.aws.amazon.com/singlesignon/latest/userguide/limits.html
            results.append(
                await apply_account_assignments(
                    identity_center_client,
                    instance_arn,
                    permission_set_arn,
                    template_account_assignments,
                    current_account_assignments,
                    log_params,
                )
            )
        except Exception as e:
            log.error("Unable to apply changes to resource", error=e, **log_params)
            return account_change_details

        # separate out the success versus failure calls
        exceptions: list[ProposedChange] = []
        changes_made: list[ProposedChange] = []
        for result in results:
            for r in result:
                if isinstance(r, ProposedChange):
                    if len(r.exceptions_seen) == 0:
                        changes_made.append(r)
                    else:
                        exceptions.append(r)

        if any(changes_made):
            account_change_details.proposed_changes.extend(changes_made)
        if any(exceptions):
            account_change_details.exceptions_seen.extend(exceptions)

        if ctx.execute:
            if any(changes_made):
                res = await boto_crud_call(
                    identity_center_client.provision_permission_set,
                    InstanceArn=instance_arn,
                    PermissionSetArn=permission_set_arn,
                    TargetType="ALL_PROVISIONED_ACCOUNTS",
                )

                request_id = res["PermissionSetProvisioningStatus"]["RequestId"]

                for _ in range(20):
                    provision_status = await boto_crud_call(
                        identity_center_client.describe_permission_set_provisioning_status,
                        InstanceArn=instance_arn,
                        ProvisionPermissionSetRequestId=request_id,
                    )

                    if (
                        provision_status["PermissionSetProvisioningStatus"]["Status"]
                        != "IN_PROGRESS"
                    ):
                        break

                    await asyncio.sleep(1)
                    continue
            if self.deleted:
                self.delete()
            self.write()  # why are writing the template here?
            log.debug(
                "Successfully finished execution on account for resource",
                changes_made=bool(account_change_details.proposed_changes),
                **log_params,
            )
        else:
            log.debug(
                "Successfully finished scanning for drift on account for resource",
                requires_changes=bool(account_change_details.proposed_changes),
                **log_params,
            )

        # If changes are detected they are to be executed then call provision_permission_set
        return account_change_details

    async def apply(self, config: AWSConfig) -> TemplateChangeDetails:
        tasks = []
        template_changes = TemplateChangeDetails(
            resource_id=self.resource_id,
            resource_type=self.resource_type,
            template_path=self.file_path,
            exceptions_seen=[],
        )
        log_params = dict(
            resource_type=self.resource_type, resource_id=self.resource_id
        )

        for account in config.accounts:
            if not account.identity_center_details:
                continue

            if evaluate_on_provider(self, account):
                if ctx.execute:
                    log_str = "Applying changes to resource."
                else:
                    log_str = "Detecting changes for resource."
                log.info(log_str, account=str(account), **log_params)
                tasks.append(self._apply_to_account(account))

        account_changes = await asyncio.gather(*tasks)
        template_changes.proposed_changes = [
            account_change
            for account_change in account_changes
            if any(account_change.proposed_changes)
        ]
        # aggregate exceptions
        template_changes.exceptions_seen = [
            account_change
            for account_change in account_changes
            if any(account_change.exceptions_seen)
        ]

        proposed_changes = [x for x in account_changes if x.proposed_changes]

        if proposed_changes and ctx.execute:
            log.info(
                "Successfully applied all or some resource changes to all AWS accounts. Any unapplied resources will have an accompanying error message.",
                **log_params,
            )
        elif proposed_changes and not ctx.execute:
            log.info(
                "Successfully detected all or some required resource changes on all AWS accounts. Any unapplied resources will have an accompanying error message.",
                **log_params,
            )
        else:
            log.debug("No changes detected for resource on any account.", **log_params)

        return template_changes

    @property
    def included_children(self):
        return []

    def set_included_children(self, value):
        pass

    @property
    def excluded_children(self):
        return []

    def set_excluded_children(self, value):
        pass

    @property
    def included_parents(self):
        return self.included_orgs

    def set_included_parents(self, value):
        self.included_orgs = value

    @property
    def excluded_parents(self):
        return self.excluded_orgs

    def set_excluded_parents(self, value):
        self.excluded_orgs = value
