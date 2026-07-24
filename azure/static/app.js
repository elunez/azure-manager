document.addEventListener("DOMContentLoaded", function () {
    "use strict";

    document.querySelectorAll(".app-toast").forEach(function (element) {
        bootstrap.Toast.getOrCreateInstance(element).show();
    });

    document.querySelectorAll("[data-submit-on-change]").forEach(function (element) {
        element.addEventListener("change", function () {
            if (element.form) {
                element.form.requestSubmit();
            }
        });
    });

    document.querySelectorAll("[data-page-jump]").forEach(function (element) {
        element.addEventListener("keydown", function (event) {
            if (event.key === "Enter" && element.form) {
                event.preventDefault();
                element.form.requestSubmit();
            }
        });
    });

    var vmTableBody = document.getElementById("vm-table-body");
    if (vmTableBody) {
        var vmCount = document.getElementById("vm-count");
        var vmRefresh = document.getElementById("vm-refresh");

        function setVmLoading() {
            vmTableBody.setAttribute("aria-busy", "true");
            vmCount.textContent = "加载中";
            vmRefresh.disabled = true;
            vmTableBody.innerHTML = [
                '<tr><td colspan="6" class="empty-state">',
                '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>',
                "正在加载 VM...",
                "</td></tr>"
            ].join("");
        }

        function setVmError(message) {
            vmTableBody.setAttribute("aria-busy", "false");
            vmCount.textContent = "加载失败";
            vmTableBody.innerHTML = [
                '<tr><td colspan="6" class="empty-state">',
                '<div class="text-danger mb-3"></div>',
                '<button class="btn btn-sm btn-warning" type="button" data-vm-retry>',
                '<i class="bi bi-arrow-clockwise" aria-hidden="true"></i> 重试',
                "</button></td></tr>"
            ].join("");
            vmTableBody.querySelector(".text-danger").textContent = message;
        }

        function loadVms() {
            setVmLoading();
            fetch(vmTableBody.dataset.vmListUrl, {
                headers: {"Accept": "application/json"}
            }).then(function (response) {
                if (response.redirected) {
                    window.location.assign(response.url);
                    return null;
                }
                return response.json().then(function (payload) {
                    if (!response.ok) {
                        throw new Error(payload.error || "VM 列表加载失败，请稍后重试");
                    }
                    return payload;
                });
            }).then(function (payload) {
                if (!payload) {
                    return;
                }
                vmTableBody.innerHTML = payload.html;
                vmTableBody.setAttribute("aria-busy", "false");
                vmCount.textContent = payload.count + " 台";
            }).catch(function (error) {
                setVmError(error.message || "VM 列表加载失败，请稍后重试");
            }).finally(function () {
                vmRefresh.disabled = false;
            });
        }

        vmRefresh.addEventListener("click", loadVms);
        vmTableBody.addEventListener("click", function (event) {
            if (event.target.closest("[data-vm-retry]")) {
                loadVms();
            }
        });
        loadVms();
    }

    var accountModal = document.getElementById("account-modal");
    if (accountModal) {
        var accountForm = document.getElementById("account-form");
        var accountTitle = document.getElementById("account-modal-title");
        var accountSecret = accountForm.querySelector('[name="client_secret"]');
        var accountSecretHint = document.getElementById("account-secret-hint");
        var accountErrors = document.getElementById("account-modal-errors");

        accountModal.addEventListener("show.bs.modal", function (event) {
            var trigger = event.relatedTarget;
            if (!trigger) {
                return;
            }

            var editMode = trigger.dataset.accountMode === "edit";
            accountTitle.textContent = editMode ? "编辑 Azure 账号" : "添加 Azure 账号";
            accountForm.action = trigger.dataset.action;
            accountForm.querySelector('[name="page"]').value = trigger.dataset.page || "1";
            accountForm.querySelector('[name="per_page"]').value = trigger.dataset.pageSize || "8";
            accountForm.querySelector('[name="account"]').value = trigger.dataset.account || "";
            accountForm.querySelector('[name="client_id"]').value = trigger.dataset.clientId || "";
            accountForm.querySelector('[name="tenant_id"]').value = trigger.dataset.tenantId || "";
            accountForm.querySelector('[name="subscription_id"]').value = trigger.dataset.subscriptionId || "";
            accountSecret.value = "";
            accountSecret.required = !editMode;
            accountSecretHint.textContent = editMode ? "留空时保留现有客户端密钥" : "保存前会验证 Azure 身份与订阅访问权限";
            if (accountErrors) {
                accountErrors.classList.add("d-none");
            }
        });

        accountModal.addEventListener("hidden.bs.modal", function () {
            if (accountModal.dataset.listUrl) {
                window.history.replaceState({}, "", accountModal.dataset.listUrl);
            }
        });

        if (accountModal.dataset.open === "true") {
            bootstrap.Modal.getOrCreateInstance(accountModal).show();
        }
    }

    var confirmModal = document.getElementById("confirm-action-modal");
    if (confirmModal) {
        confirmModal.addEventListener("show.bs.modal", function (event) {
            var trigger = event.relatedTarget;
            var sourceForm = trigger ? trigger.closest("form") : null;
            if (!trigger || !sourceForm) {
                event.preventDefault();
                return;
            }

            var targetForm = document.getElementById("confirm-action-form");
            var fields = document.getElementById("confirm-action-fields");
            targetForm.action = sourceForm.action;
            fields.replaceChildren();
            sourceForm.querySelectorAll('input[type="hidden"]').forEach(function (input) {
                fields.appendChild(input.cloneNode(true));
            });
            document.getElementById("confirm-action-title").textContent = trigger.dataset.confirmTitle || "确认操作";
            document.getElementById("confirm-action-body").textContent = trigger.dataset.confirmBody || "确定继续执行此操作？";
            document.getElementById("confirm-action-submit").className = "btn " + (trigger.dataset.confirmClass || "btn-danger");
        });
    }

    var failureDetailModal = document.getElementById("failure-detail-modal");
    if (failureDetailModal) {
        failureDetailModal.addEventListener("show.bs.modal", function (event) {
            var trigger = event.relatedTarget;
            if (!trigger || trigger.disabled) {
                event.preventDefault();
                return;
            }
            document.getElementById("failure-detail-content").textContent =
                trigger.dataset.failureDetail || "未知失败原因";
        });
    }
});
