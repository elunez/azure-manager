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

    document.querySelectorAll("[data-settings-tab]").forEach(function (element) {
        element.addEventListener("shown.bs.tab", function () {
            var url = new URL(window.location.href);
            url.searchParams.set("tab", element.dataset.settingsTab);
            window.history.replaceState({}, "", url.toString());
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

        function loadVms(forceRefresh) {
            setVmLoading();
            var vmListUrl = new URL(vmTableBody.dataset.vmListUrl, window.location.origin);
            if (forceRefresh) {
                vmListUrl.searchParams.set("refresh", "1");
            }
            fetch(vmListUrl.toString(), {
                cache: "no-store",
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

        vmRefresh.addEventListener("click", function () {
            loadVms(true);
        });
        vmTableBody.addEventListener("click", function (event) {
            if (event.target.closest("[data-vm-retry]")) {
                loadVms(false);
            }
        });
        loadVms(false);
    }

    var costContent = document.getElementById("cost-overview-content");
    if (costContent) {
        var costRefresh = document.getElementById("cost-refresh");
        var costChartElement = null;
        var costChartFrame = null;
        var svgNamespace = "http://www.w3.org/2000/svg";

        function createSvgElement(tagName, attributes, text) {
            var element = document.createElementNS(svgNamespace, tagName);
            Object.keys(attributes || {}).forEach(function (name) {
                element.setAttribute(name, attributes[name]);
            });
            if (text !== undefined) {
                element.textContent = text;
            }
            return element;
        }

        function renderCostLineChart(chart) {
            var points;
            try {
                points = JSON.parse(chart.dataset.chartPoints || "[]");
            } catch (error) {
                points = [];
            }
            if (!points.length) {
                chart.replaceChildren();
                return;
            }

            var chartContainer = chart.closest(".cost-line-chart-wrap");
            var chartWidth = Math.max(chartContainer.clientWidth, 280);
            var chartHeight = 220;
            var plotLeft = 54;
            var plotRight = 20;
            var plotTop = 16;
            var plotBottom = 174;
            var labelY = 207;
            var plotWidth = chartWidth - plotLeft - plotRight;
            var pointSpacing = plotWidth / Math.max(1, points.length - 1);
            var maxDateLabels = Math.max(2, Math.floor(plotWidth / 48) + 1);
            var visibleDateLabels = Math.min(points.length, maxDateLabels);
            var dateLabelIndexes = new Set();
            if (visibleDateLabels === 1) {
                dateLabelIndexes.add(0);
            } else {
                for (var labelIndex = 0; labelIndex < visibleDateLabels; labelIndex += 1) {
                    dateLabelIndexes.add(Math.round(
                        labelIndex * (points.length - 1) / (visibleDateLabels - 1)
                    ));
                }
            }
            var minCost = Number(chart.dataset.chartMin || 0);
            var maxCost = Number(chart.dataset.chartMax || 0);
            var zeroRatio = Number(chart.dataset.chartZeroRatio || 0.5);
            var currency = chart.dataset.chartCurrency || "";

            function pointX(index) {
                return points.length === 1
                    ? chartWidth / 2
                    : plotLeft + pointSpacing * index;
            }

            function pointY(ratio) {
                var normalized = Math.min(1, Math.max(0, Number(ratio)));
                return plotTop + normalized * (plotBottom - plotTop);
            }

            chart.style.width = Math.ceil(chartWidth) + "px";
            chart.setAttribute("viewBox", "0 0 " + chartWidth + " " + chartHeight);
            chart.replaceChildren();

            [plotTop, plotBottom].forEach(function (y) {
                chart.appendChild(createSvgElement("line", {
                    "class": "cost-chart-grid-line",
                    "x1": plotLeft,
                    "x2": chartWidth - plotRight,
                    "y1": y,
                    "y2": y
                }));
            });

            var zeroY = pointY(zeroRatio);
            chart.appendChild(createSvgElement("line", {
                "class": "cost-chart-grid-line cost-chart-zero-line",
                "x1": plotLeft,
                "x2": chartWidth - plotRight,
                "y1": zeroY,
                "y2": zeroY
            }));

            if (maxCost === minCost) {
                chart.appendChild(createSvgElement("text", {
                    "class": "cost-chart-axis-label",
                    "x": 4,
                    "y": zeroY + 4
                }, maxCost.toFixed(2)));
            } else {
                chart.appendChild(createSvgElement("text", {
                    "class": "cost-chart-axis-label",
                    "x": 4,
                    "y": plotTop + 4
                }, maxCost.toFixed(2)));
                chart.appendChild(createSvgElement("text", {
                    "class": "cost-chart-axis-label",
                    "x": 4,
                    "y": plotBottom + 4
                }, minCost.toFixed(2)));
            }

            var coordinates = points.map(function (point, index) {
                return pointX(index) + "," + pointY(point.chart_ratio);
            });
            chart.appendChild(createSvgElement("polyline", {
                "class": "cost-chart-line",
                "points": coordinates.join(" ")
            }));

            points.forEach(function (point, index) {
                var x = pointX(index);
                var y = pointY(point.chart_ratio);
                var amount = Number(point.cost || 0).toFixed(2);
                var pointLabel = point.date + "：" + amount + " " + currency;
                var circle = createSvgElement("circle", {
                    "class": "cost-chart-point",
                    "cx": x,
                    "cy": y,
                    "r": 4,
                    "tabindex": 0,
                    "aria-label": pointLabel
                });
                circle.appendChild(createSvgElement("title", {}, pointLabel));
                chart.appendChild(circle);
                if (dateLabelIndexes.has(index)) {
                    chart.appendChild(createSvgElement("text", {
                        "class": "cost-chart-date-label",
                        "x": x,
                        "y": labelY
                    }, point.date.slice(5)));
                }
            });
        }

        function initializeCostChart() {
            costChartElement = costContent.querySelector("[data-cost-line-chart]");
            if (costChartElement) {
                renderCostLineChart(costChartElement);
            }
        }

        function setCostLoading() {
            costChartElement = null;
            costContent.setAttribute("aria-busy", "true");
            costRefresh.disabled = true;
            costContent.innerHTML = [
                '<div class="cost-loading-state">',
                '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>',
                "正在加载费用数据...",
                "</div>"
            ].join("");
        }

        function setCostError(message, retryAfter) {
            costChartElement = null;
            costContent.setAttribute("aria-busy", "false");
            costContent.innerHTML = [
                '<div class="cost-error-state">',
                '<i class="bi bi-exclamation-circle" aria-hidden="true"></i>',
                '<div class="text-danger"></div>',
                '<button class="btn btn-sm btn-warning" type="button" data-cost-retry>',
                '<i class="bi bi-arrow-clockwise" aria-hidden="true"></i> 重试',
                "</button></div>"
            ].join("");
            var errorMessage = costContent.querySelector(".text-danger");
            errorMessage.textContent = message;
            if (retryAfter > 0) {
                var retryButton = costContent.querySelector("[data-cost-retry]");
                var retryDeadline = Date.now() + retryAfter * 1000;
                retryButton.disabled = true;
                costRefresh.disabled = true;

                function updateRetryButton() {
                    var seconds = Math.max(
                        0,
                        Math.ceil((retryDeadline - Date.now()) / 1000)
                    );
                    if (seconds <= 0) {
                        errorMessage.textContent = "Azure 费用接口限流已解除，可以重新尝试";
                        retryButton.textContent = "重试";
                        retryButton.disabled = false;
                        costRefresh.disabled = false;
                        return;
                    }
                    errorMessage.textContent = (
                        "Azure 费用接口正在限流，请在 " + seconds + " 秒后重试"
                    );
                    retryButton.textContent = seconds + " 秒后可重试";
                    window.setTimeout(updateRetryButton, 1000);
                }

                updateRetryButton();
            }
        }

        function loadCosts(forceRefresh) {
            setCostLoading();
            var rateLimited = false;
            var url = new URL(costContent.dataset.costUrl, window.location.href);
            if (forceRefresh) {
                url.searchParams.set("refresh", "1");
            }
            fetch(url.toString(), {
                headers: {"Accept": "application/json"}
            }).then(function (response) {
                if (response.redirected) {
                    window.location.assign(response.url);
                    return null;
                }
                return response.json().then(function (payload) {
                    if (!response.ok) {
                        var requestError = new Error(payload.error || "费用数据加载失败，请稍后重试");
                        requestError.retryAfter = Number(payload.retry_after || 0);
                        throw requestError;
                    }
                    return payload;
                });
            }).then(function (payload) {
                if (!payload) {
                    return;
                }
                costContent.innerHTML = payload.html;
                costContent.setAttribute("aria-busy", "false");
                initializeCostChart();
            }).catch(function (error) {
                rateLimited = error.retryAfter > 0;
                setCostError(
                    error.message || "费用数据加载失败，请稍后重试",
                    error.retryAfter || 0
                );
            }).finally(function () {
                if (!rateLimited) {
                    costRefresh.disabled = false;
                }
            });
        }

        costRefresh.addEventListener("click", function () {
            loadCosts(true);
        });
        costContent.addEventListener("click", function (event) {
            if (event.target.closest("[data-cost-retry]")) {
                loadCosts(true);
            }
        });
        window.addEventListener("resize", function () {
            if (!costChartElement || !costChartElement.isConnected) {
                return;
            }
            if (costChartFrame) {
                window.cancelAnimationFrame(costChartFrame);
            }
            costChartFrame = window.requestAnimationFrame(function () {
                renderCostLineChart(costChartElement);
                costChartFrame = null;
            });
        });
        loadCosts(false);
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
            accountForm.querySelector('[name="per_page"]').value = trigger.dataset.pageSize || "10";
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
