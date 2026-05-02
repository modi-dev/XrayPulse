// ===============================================
// XRAYPULSE - CLIENT SIDE LOGIC ENGINE (v2.0)
// responsible for fetching data, rendering charts, and populating dashboard cards.
// ===============================================

const LS_PERIOD = 'xraypulse_period';
const LS_REFRESH = 'xraypulse_refresh_ms';

let dashboardRefreshIntervalId = null;
// Инстансы для Chart.js, чтобы их можно было уничтожать и пересоздавать при обновлении данных
let mainChartInstance = null;

/** Состояние пагинации /api/history (первая страница + «ещё»). */
const historyState = {
    items: [],
    nextCursor: null,
    hasMore: false,
    summary: null,
};

/** Защита от параллельных loadData (двойной «Загрузить ещё», таймер + кнопка). */
let historyLoadLocked = false;

function getSelectedPeriod() {
    const select = document.getElementById('periodFilter');
    return select?.value || '7d';
}

function escapeHtmlText(s) {
    return String(s ?? '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
}

function formatDisplayTime(isoOrLegacy) {
    if (!isoOrLegacy || typeof isoOrLegacy !== 'string') return '';
    const d = new Date(isoOrLegacy);
    if (!Number.isNaN(d.getTime())) {
        return d.toLocaleString('ru-RU', { hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }
    return isoOrLegacy;
}

function ownerBadge(owner, asn = null) {
    if (!owner) return '';
    const ownerLower = owner.toLowerCase();
    let badgeClass = 'bg-violet-900/40 text-violet-300 border-violet-500/40';
    let label = 'Public ASN';

    if (ownerLower.includes('private/local')) {
        badgeClass = 'bg-gray-800 text-gray-300 border-gray-600';
        label = 'Private/Local';
    } else if (ownerLower.includes('telegram')) {
        badgeClass = 'bg-blue-900/40 text-blue-300 border-blue-500/40';
        label = 'Telegram';
    }

    return `<span class="inline-flex items-center px-2 py-0.5 rounded border text-[10px] font-semibold ${badgeClass}">
        ${label}${asn ? ` · ${asn}` : ''}
    </span>`;
}

/**
 * Утилиты: Группировка и подсчет инцидентов (для таблицы)
 */
function groupLogs(rawData) {
    const groups = [];
    // Используем Map для более эффективного отслеживания уникальных комбинаций
    const groupMap = new Map();

    rawData.forEach(item => {
        // Проверка на наличие времени перед обработкой
        if (!item || typeof item.time === 'undefined') return; 

        // Ключ группировки — текст ошибки + описание (убираем пробелы и спецсимволы)
        const key = `${item.type}|${item.desc}`; 
        
        if (!groupMap.has(key)) {
            const group = {
                ...item, // Данные последней записи
                count: 1,
                instances: [item] // Храним все записи этой группы
            };
            groupMap.set(key, group);
            groups.push(group);
        } else {
            const existingGroup = groupMap.get(key);
            existingGroup.count += 1;
            existingGroup.instances.push(item);
        }
    });
    return groups;
}

/**
 * 1. Рендеринг Главного Графика (Time Series Trend) - Улучшена защита от null/undefined данных!
 */
function renderMainChart(data) {
    const canvas = document.getElementById('mainChart');
    if (!canvas) return;

    const selectedPeriod = getSelectedPeriod();
    // Для коротких периодов делаем более детальную разбивку по минутам.
    const periodConfig = {
        '1h': { bucketMinutes: 1, bucketCount: 60 },
        '6h': { bucketMinutes: 5, bucketCount: 72 },
        '24h': { bucketMinutes: 30, bucketCount: 48 },
        '7d': { bucketMinutes: 60, bucketCount: 24 * 7 },
        '30d': { bucketMinutes: 180, bucketCount: 24 * 10 }
    };
    const cfg = periodConfig[selectedPeriod] || periodConfig['7d'];
    const bucketMs = cfg.bucketMinutes * 60 * 1000;

    const bucketMap = new Map();
    const parseLogTime = (value) => {
        if (!value || typeof value !== 'string') return null;
        if (value.includes('T') && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(value)) {
            const d = new Date(value);
            return Number.isNaN(d.getTime()) ? null : d;
        }
        const m = value.match(
            /^(\d{4})[/-](\d{2})[/-](\d{2}) (\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$/
        );
        if (m) {
            const [, y, mo, d, h, mi, s, frac = '0'] = m;
            const ms = Number((frac + '000').slice(0, 3));
            return new Date(
                Number(y),
                Number(mo) - 1,
                Number(d),
                Number(h),
                Number(mi),
                Number(s),
                ms
            );
        }

        // Фоллбек для редких нестандартных строк: нормализуем ISO-подобный формат.
        const isoLike = value.replace(/\//g, '-').replace(' ', 'T');
        const fallback = new Date(isoLike);
        return Number.isNaN(fallback.getTime()) ? null : fallback;
    };

    const parsedDates = data
        .filter(item => item && typeof item.time === 'string')
        .map(item => parseLogTime(item.time))
        .filter(Boolean);
    const now = parsedDates.length ? new Date(Math.max(...parsedDates.map(d => d.getTime()))) : new Date();

    const alignToBucketStart = (dt) => {
        const ts = dt.getTime();
        return new Date(Math.floor(ts / bucketMs) * bucketMs);
    };
    const formatBucketKey = (dt) => (
        `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')} ` +
        `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
    );

    const nowBucketStart = alignToBucketStart(now);
    for (let i = cfg.bucketCount - 1; i >= 0; i--) {
        const dt = new Date(nowBucketStart.getTime() - i * bucketMs);
        const key = formatBucketKey(dt);
        bucketMap.set(key, 0);
    }

    data.filter(item => item && typeof item.time === 'string').forEach(item => {
        const dt = parseLogTime(item.time);
        if (!dt) return;
        const key = formatBucketKey(alignToBucketStart(dt));
        if (bucketMap.has(key)) {
            bucketMap.set(key, (bucketMap.get(key) || 0) + 1);
        }
    });

    const labels = Array.from(bucketMap.keys());
    const counts = labels.map(label => bucketMap.get(label));
    const maxCount = counts.length ? Math.max(...counts) : 0;
    const peakThreshold = maxCount > 0 ? Math.max(2, Math.ceil(maxCount * 0.7)) : 1;
    const pointColors = counts.map(c => (c >= peakThreshold ? '#ef4444' : '#60a5fa'));
    const compactTimePeriods = new Set(['1h', '6h']);
    const renderTickLabel = (idx) => {
        const current = labels[idx];
        if (!current) return '';
        const [datePart, hourPart] = current.split(' ');
        const [, month, day] = datePart.split('-');
        const prevDate = idx > 0 ? labels[idx - 1]?.split(' ')[0] : null;
        const isDateBoundary = idx === 0 || prevDate !== datePart;

        if (compactTimePeriods.has(selectedPeriod)) {
            // Для коротких периодов оставляем компактное время и показываем дату только при смене дня.
            return isDateBoundary ? `${hourPart}\n${day}.${month}` : hourPart;
        }
        return `${day}.${month} ${hourPart}`;
    };

    if (mainChartInstance) {
        mainChartInstance.destroy();
    }

    mainChartInstance = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
            labels,
            datasets: [{
                label: 'Количество инцидентов', 
                data: counts,
                backgroundColor: 'rgba(59, 130, 246, 0.7)', // blue-600/70%
                borderColor: 'rgba(59, 130, 246, 1)',
                borderWidth: 1,
                pointBackgroundColor: pointColors
            }, {
                label: 'Пики (>= 70% от max)',
                type: 'line',
                data: counts.map(v => (v >= peakThreshold ? v : null)),
                borderColor: '#ef4444',
                backgroundColor: '#ef4444',
                pointRadius: 4,
                pointHoverRadius: 6,
                tension: 0.2
            }]
        },
        options: {
            indexAxis: 'x',
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { 
                    grid: { color: '#374151' }, 
                    beginAtZero: true,
                    suggestedMax: Math.max(2, maxCount + 1),
                    ticks: { color: '#9CA3AF' }
                },
                x: {
                    grid: { display: false },
                    ticks: {
                        color: '#9CA3AF',
                        maxRotation: 0,
                        minRotation: 0,
                        autoSkip: true,
                        maxTicksLimit: compactTimePeriods.has(selectedPeriod) ? 10 : 12,
                        callback: (_, index) => renderTickLabel(index)
                    }
                }
            }, 
            plugins: {
                legend: { display: true, position: 'top' },
                title: { display: true, text: `Тренд по времени (${selectedPeriod})` },
                tooltip: {
                    callbacks: {
                        title: (tooltipItems) => {
                            const idx = tooltipItems?.[0]?.dataIndex ?? 0;
                            return labels[idx];
                        },
                        afterLabel: (context) => {
                            const value = context.parsed.y;
                            return value >= peakThreshold ? 'Возможный всплеск' : 'Нормальный уровень';
                        }
                    }
                }
            }
        }
    });
}

/**
 * 2. Таблица топа причин — из агрегата API /api/error-types (весь период, не только текущая страница истории).
 */
function renderTypeChartFromApi(aggregatedRows) {
    const container = document.getElementById('typeSummary');
    if (!container) return;

    const rows = Array.isArray(aggregatedRows) ? aggregatedRows : [];
    const topTypes = rows.slice(0, 12);

    if (!topTypes.length) {
        container.innerHTML = '<div class="text-gray-400">Нет данных по ошибкам.</div>';
        return;
    }

    const rowsHtml = topTypes.map((row, idx) => {
        const typeId = row.id;
        const errorType = row.error_type || '';
        const description = row.description || 'Нет описания';
        const count = row.count ?? 0;
        const safeErrorType = JSON.stringify(errorType);
        return `
            <button type="button" class="w-full text-left grid grid-cols-[40px_minmax(220px,1fr)_minmax(320px,2fr)_110px] gap-3 px-3 py-2 rounded hover:bg-gray-700/60 transition border border-gray-700 items-start"
                onclick='showErrorTypeDetails(${Number(typeId)}, ${safeErrorType})'
                >
                <span class="text-xs text-gray-400">${idx + 1}</span>
                <span class="text-sm text-gray-300 whitespace-normal break-words leading-snug">${description}</span>
                <span class="text-sm text-gray-100 whitespace-normal break-words leading-snug">${errorType}</span>
                <span class="text-sm text-blue-300 font-mono text-right">${count}</span>
            </button>
        `;
    }).join('');

    container.innerHTML = `
        <div class="grid grid-cols-[40px_minmax(220px,1fr)_minmax(320px,2fr)_110px] gap-3 px-3 pb-2 text-xs uppercase text-gray-400 border-b border-gray-700">
            <span>#</span><span>Причина</span><span>Текст ошибки</span><span class="text-right">Кол-во</span>
        </div>
        <div class="mt-2 space-y-2">${rowsHtml}</div>
    `;
}

function checkedValuesFromListRoot(listRoot) {
    if (!listRoot) return new Set();
    const set = new Set();
    listRoot.querySelectorAll('input[type="checkbox"]').forEach((cb) => {
        if (cb.checked && cb.value) set.add(cb.value);
    });
    return set;
}

/** Скрывает строки чекбоксов, не совпадающие с полем поиска в той же панели. */
function applyMultiddListSearch(wrap) {
    if (!wrap) return;
    const inp = wrap.querySelector('.filter-multidd-search');
    const listRoot = wrap.querySelector('.filter-multidd-list');
    if (!listRoot) return;
    const q = (inp?.value || '').trim().toLowerCase();
    listRoot.querySelectorAll('label').forEach((lab) => {
        const txt = (lab.textContent || '').toLowerCase();
        lab.classList.toggle('hidden', q.length > 0 && !txt.includes(q));
    });
}

function updateFilterMultiddSummary(wrap) {
    if (!wrap) return;
    const summary = wrap.querySelector('.filter-multidd-summary');
    const listRoot = wrap.querySelector('.filter-multidd-list');
    const trigger = wrap.querySelector('.filter-multidd-trigger');
    if (!summary || !listRoot) return;
    const emptyText = wrap.dataset.emptySummary || 'Все';
    const checked = listRoot.querySelectorAll('input[type="checkbox"]:checked');
    const n = checked.length;
    if (n === 0) {
        summary.textContent = emptyText;
    } else if (n === 1) {
        const lab = checked[0].closest('label')?.querySelector('.filter-multidd-cblabel');
        const t = lab?.textContent?.trim() || checked[0].value;
        summary.textContent = t.length > 48 ? `${t.slice(0, 45)}…` : t;
    } else {
        summary.textContent = `Выбрано: ${n}`;
    }
    const panel = wrap.querySelector('.filter-multidd-panel');
    if (trigger && panel) {
        trigger.setAttribute('aria-expanded', panel.classList.contains('hidden') ? 'false' : 'true');
    }
}

/**
 * Перезаполняет список чекбоксов, сохраняя выбор, если значения есть в новом наборе.
 */
function repopulateCheckboxFilterList(listRoot, items, toOption) {
    if (!listRoot) return;
    const prev = checkedValuesFromListRoot(listRoot);
    listRoot.innerHTML = '';
    (Array.isArray(items) ? items : []).forEach((item) => {
        const { value, label } = toOption(item);
        if (!value && value !== 0) return;
        const v = String(value);
        const lab = document.createElement('label');
        lab.className = 'flex cursor-pointer items-start gap-2 rounded px-2 py-1.5 text-sm hover:bg-gray-700/80';
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = v;
        input.className = 'filter-multidd-cb mt-0.5 shrink-0 rounded border-gray-600 bg-gray-900 text-blue-500 focus:ring-blue-500';
        if (prev.has(v)) input.checked = true;
        const span = document.createElement('span');
        span.className = 'filter-multidd-cblabel min-w-0 flex-1 break-words leading-snug text-gray-200';
        span.textContent = label;
        lab.appendChild(input);
        lab.appendChild(span);
        listRoot.appendChild(lab);
        input.addEventListener('change', () => updateFilterMultiddSummary(listRoot.closest('.filter-multidd')));
    });
    const wrap = listRoot.closest('.filter-multidd');
    updateFilterMultiddSummary(wrap);
    applyMultiddListSearch(wrap);
}

function populateHistoryFilters(typeRows, picklists) {
    repopulateCheckboxFilterList(
        document.getElementById('filterErrorTypesList'),
        typeRows,
        (row) => ({
            value: row.id,
            label: `${(row.description || 'Тип').slice(0, 72)} (${row.count ?? 0})`,
        }),
    );
    repopulateCheckboxFilterList(
        document.getElementById('filterDestinationsList'),
        picklists?.destinations || [],
        (d) => ({ value: d, label: d }),
    );
}

function closeAllFilterMultiddPanels() {
    document.querySelectorAll('.filter-multidd').forEach((wrap) => {
        const panel = wrap.querySelector('.filter-multidd-panel');
        const trigger = wrap.querySelector('.filter-multidd-trigger');
        if (panel) panel.classList.add('hidden');
        if (trigger) trigger.setAttribute('aria-expanded', 'false');
    });
}

function initFilterMultiddUi() {
    document.querySelectorAll('.filter-multidd').forEach((wrap) => {
        const trigger = wrap.querySelector('.filter-multidd-trigger');
        const panel = wrap.querySelector('.filter-multidd-panel');
        if (!trigger || !panel) return;
        trigger.addEventListener('click', (e) => {
            e.stopPropagation();
            const wasOpen = !panel.classList.contains('hidden');
            closeAllFilterMultiddPanels();
            if (!wasOpen) {
                panel.classList.remove('hidden');
                trigger.setAttribute('aria-expanded', 'true');
                const searchInp = wrap.querySelector('.filter-multidd-search');
                if (searchInp) {
                    requestAnimationFrame(() => searchInp.focus());
                }
            }
        });
        const clearBtn = wrap.querySelector('.filter-multidd-clear');
        if (clearBtn) {
            clearBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                wrap.querySelectorAll('.filter-multidd-list input[type="checkbox"]').forEach((cb) => {
                    cb.checked = false;
                });
                const searchInp = wrap.querySelector('.filter-multidd-search');
                if (searchInp) searchInp.value = '';
                applyMultiddListSearch(wrap);
                updateFilterMultiddSummary(wrap);
            });
        }
        const searchInp = wrap.querySelector('.filter-multidd-search');
        if (searchInp) {
            searchInp.addEventListener('click', (e) => e.stopPropagation());
            searchInp.addEventListener('input', () => applyMultiddListSearch(wrap));
        }
    });
    document.addEventListener('click', () => {
        closeAllFilterMultiddPanels();
    });
    document.querySelectorAll('.filter-multidd').forEach((wrap) => {
        wrap.addEventListener('click', (e) => e.stopPropagation());
    });
}


/**
 * 3. Обновление KPI (агрегаты с бэкенда по фильтрам периода).
 */
function updateKpiCards(summary) {
    const s = summary && typeof summary === 'object' ? summary : {};
    const totalElement = document.getElementById('kpi-total-logs');
    const newErrorsElement = document.getElementById('kpi-new-errors');
    const criticalErrorElement = document.getElementById('kpi-critical-errors');

    if (totalElement) totalElement.innerText = s.total ?? '—';
    if (newErrorsElement) newErrorsElement.innerText = s.distinct_types ?? '—';
    if (criticalErrorElement) criticalErrorElement.innerText = s.distinct_sources ?? '—';
}

/**
 * 4. Рендеринг Таблицы и Обработка Интерактивности
 */
function renderTable(rawData) {
    const tbody = document.getElementById('tableBody');
    if (!tbody) return;

    const groupedData = groupLogs(rawData);
    window.__lastHistoryGroups = groupedData;

    // Очистка перед рендерингом
    tbody.innerHTML = ''; 

    groupedData.forEach((group, index) => {
        // --- Основная строка лога (Visible row) ---
        const countBadge = group.count > 1
            ? `<span class="shrink-0 px-2 py-0.5 bg-blue-900/50 text-blue-400 border border-blue-500/30 rounded text-[10px] font-bold">×${group.count}</span>`
            : '';

        const row = document.createElement('tr');
        row.className = 'border-b border-gray-700 hover:bg-gray-750 transition cursor-pointer';
        row.setAttribute('onclick', `handleHistoryRowClick(event, ${index})`);
        
        // Используем Template literals для чистой вставки HTML
        row.innerHTML = `
            <td class="px-4 py-3 text-xs text-blue-300 font-mono whitespace-nowrap">${formatDisplayTime(group.time)}</td>
            <td class="px-4 py-3 text-xs text-green-400 font-mono">
                <div class="whitespace-normal break-words">${group.source}</div>
                ${group.source_location ? `<div class="text-[11px] text-gray-400 mt-1">${group.source_location}</div>` : ''}
                ${group.source_owner ? `<div class="text-[11px] text-blue-300 mt-1">Owner: ${group.source_owner}</div>` : ''}
                ${group.source_owner ? `<div class="mt-1">${ownerBadge(group.source_owner, group.source_asn)}</div>` : ''}
            </td>
            <td class="px-4 py-3 text-xs text-yellow-200 font-mono">${group.destination}</td>
            <td class="px-4 py-3 text-xs text-red-400 font-mono align-top">
                <div class="flex items-start justify-between gap-2 min-w-0">
                    <span class="min-w-0 flex-1 whitespace-normal break-words leading-snug">${group.type}</span>
                    ${countBadge}
                </div>
            </td>
            <td class="px-4 py-3 text-sm text-gray-300">${group.desc}</td>
            <td class="px-4 py-3 text-center align-top">
                <button type="button" class="copy-raw-btn text-xs text-cyan-400 hover:text-cyan-200 underline" data-group-index="${index}" data-inst-index="0">Копировать</button>
            </td>
        `;
        tbody.appendChild(row);

        // --- Скрытый блок деталей (Hidden row) ---
        const detailsSection = document.createElement('tr');
        detailsSection.id = `details-${index}`;
        detailsSection.className = 'hidden bg-black/30 border-b border-gray-800';
        
        // Генерация списка всех инцидентов в группе
        const instancesHtml = group.instances.map((inst, j) => `
            <div class="flex flex-wrap gap-2 items-center py-1 border-b border-gray-800/50 last:border-0 hover:text-gray-200 text-xs">
                <span class="font-mono text-blue-400/80 shrink-0">${formatDisplayTime(inst.time)}</span>
                <span class="font-mono text-green-500/70 shrink-0">${inst.source}</span>
                <span class="font-mono text-gray-500 flex-1 min-w-0 whitespace-normal break-words">${inst.type}</span>
                <button type="button" class="copy-raw-btn shrink-0 text-cyan-400 hover:text-cyan-200 underline" data-group-index="${index}" data-inst-index="${j}">Копировать</button>
            </div>
        `).join('');

        detailsSection.innerHTML = `
            <td colspan="6" class="px-6 py-4">
                <div class="text-[11px] text-gray-400 mb-2 uppercase font-bold tracking-wider">Все ${group.count} вхождения этой группы:</div
                ><div class="max-h-40 overflow-y-auto space-y-1 pr-2 custom-scrollbar">${instancesHtml}</div>
            </td>
        `;
        tbody.appendChild(detailsSection);
    });
}

// Глобальная функция для переключения видимости (обязательно на window)
window.toggleDetails = function(index) {
    const el = document.getElementById(`details-${index}`);
    if (el) el.classList.toggle('hidden');
};

window.handleHistoryRowClick = function (ev, index) {
    if (ev && ev.target && ev.target.closest('.copy-raw-btn')) return;
    window.toggleDetails(index);
};

window.openErrorTypeDrawer = function() {
    const drawer = document.getElementById('errorTypeDetailsDrawer');
    const backdrop = document.getElementById('errorDetailsBackdrop');
    if (!drawer || !backdrop) return;
    backdrop.classList.remove('hidden');
    drawer.classList.remove('translate-x-full');
};

window.closeErrorTypeDrawer = function() {
    const drawer = document.getElementById('errorTypeDetailsDrawer');
    const backdrop = document.getElementById('errorDetailsBackdrop');
    if (!drawer || !backdrop) return;
    drawer.classList.add('translate-x-full');
    backdrop.classList.add('hidden');
};

window.showErrorTypeDetails = async function(errorTypeId, errorTypeName = '') {
    const detailsTitle = document.getElementById('errorTypeDetailsTitle');
    const detailsBody = document.getElementById('errorTypeDetailsBody');
    if (!detailsBody || !detailsTitle) return;

    openErrorTypeDrawer();
    detailsTitle.textContent = errorTypeName || 'Тип ошибки';
    detailsBody.innerHTML = '<div class="text-gray-400">Загрузка...</div>';

    try {
        const period = getSelectedPeriod();
        const response = await fetch(`/api/error-types/${errorTypeId}/events?period=${encodeURIComponent(period)}`);
        if (!response.ok) throw new Error(`Ошибка сервера: ${response.status}`);
        const events = await response.json();
        if (!events.length) {
            detailsBody.innerHTML = '<div class="text-gray-400">Для этого типа ошибок нет событий.</div>';
            return;
        }

        window.__lastDrawerEvents = events;
        detailsBody.innerHTML = events.map((evt, idx) => `
            <div class="py-3 border-b border-gray-700 last:border-0">
                <button type="button" class="w-full text-left" onclick="toggleRawMessage(${idx})">
                    <div class="text-sm text-blue-300 font-mono">${formatDisplayTime(evt.timestamp) || 'N/A'}</div>
                    <div class="text-sm text-gray-200 mt-1">Источник: ${evt.source_ip || 'N/A'}${evt.source_port ? ':' + evt.source_port : ''}</div>
                    ${evt.source_location ? `<div class="text-sm text-gray-400">Локация: ${evt.source_location}</div>` : ''}
                    ${evt.source_owner ? `<div class="text-sm text-blue-300">Owner: ${evt.source_owner}</div>` : ''}
                    ${evt.source_owner ? `<div class="mt-1">${ownerBadge(evt.source_owner, evt.source_asn)}</div>` : ''}
                    <div class="text-sm text-gray-200">Назначение: ${evt.destination_host || 'N/A'}${evt.destination_port ? ':' + evt.destination_port : ''}</div>
                    ${evt.destination_owner ? `<div class="text-sm text-cyan-300">Destination owner: ${evt.destination_owner}</div>` : ''}
                    ${evt.destination_owner ? `<div class="mt-1">${ownerBadge(evt.destination_owner, evt.destination_asn)}</div>` : ''}
                    <div class="text-xs text-blue-400 mt-1">Показать исходное сообщение</div>
                </button>
                <div class="mt-2 flex justify-end">
                    <button type="button" class="text-xs text-cyan-400 hover:text-cyan-200 underline" onclick="copyDrawerRaw(${idx})">Копировать сырую строку</button>
                </div>
                <div id="raw-msg-${idx}" class="hidden text-sm text-gray-400 mt-2 break-words bg-gray-800/70 rounded p-2 border border-gray-700">
                    ${escapeHtmlText(evt.raw_message || 'Нет исходного сообщения')}
                </div>
            </div>
        `).join('');
    } catch (err) {
        detailsBody.innerHTML = `<div class="text-red-400">Ошибка загрузки деталей: ${err.message}</div>`;
    }
};

window.toggleRawMessage = function(index) {
    const block = document.getElementById(`raw-msg-${index}`);
    if (block) block.classList.toggle('hidden');
};

window.copyDrawerRaw = function (idx) {
    const text = window.__lastDrawerEvents?.[idx]?.raw_message;
    if (!text) return;
    navigator.clipboard.writeText(text).catch(() => {});
};

function appendCheckedListParam(params, key, listRootId) {
    const root = document.getElementById(listRootId);
    if (!root) return;
    root.querySelectorAll('input[type="checkbox"]:checked').forEach((cb) => {
        if (cb.value) params.append(key, cb.value);
    });
}

function buildHistoryUrl(append) {
    const period = getSelectedPeriod();
    const params = new URLSearchParams();
    params.set('period', period);
    params.set('limit', '500');
    if (append && historyState.nextCursor) {
        params.set('cursor', historyState.nextCursor);
    }
    appendCheckedListParam(params, 'error_type_id', 'filterErrorTypesList');
    const sip = document.getElementById('filterSourceIpSearch')?.value?.trim();
    if (sip) params.set('source_ip', sip);
    appendCheckedListParam(params, 'destination', 'filterDestinationsList');
    return `/api/history?${params.toString()}`;
}

/**
 * 5. Основная асинхронная функция загрузки данных (CORE LOGIC)
 * @param {{ append?: boolean, resetPaging?: boolean }} options
 */
async function loadData(options = {}) {
    if (historyLoadLocked) {
        return;
    }
    const append = Boolean(options.append);
    const resetPaging = options.resetPaging !== false;

    if (append && !historyState.nextCursor) {
        return;
    }

    historyLoadLocked = true;
    const loadMoreBtn = document.getElementById('historyLoadMore');
    if (loadMoreBtn && append) {
        loadMoreBtn.disabled = true;
    }

    try {
        console.time('API_FETCH');
        const period = getSelectedPeriod();
        if (!append && resetPaging) {
            historyState.nextCursor = null;
        }
        const historyUrl = buildHistoryUrl(append);
        const typesUrl = `/api/error-types?period=${encodeURIComponent(period)}`;

        const histRes = await fetch(historyUrl);
        if (!histRes.ok) throw new Error(`Ошибка сервера: ${histRes.status}`);
        const payload = await histRes.json();

        if (payload.error) {
            throw new Error(payload.error || 'Ошибка API');
        }

        let typeRows = [];
        let picklists = { destinations: [] };
        try {
            const optUrl = `/api/filter-options?period=${encodeURIComponent(period)}`;
            const [typesRes, optRes] = await Promise.all([
                fetch(typesUrl),
                fetch(optUrl),
            ]);
            if (typesRes.ok) typeRows = await typesRes.json();
            if (optRes.ok) picklists = await optRes.json();
        } catch (e) {
            console.warn('error-types / filter-options fetch failed', e);
        }

        console.timeEnd('API_FETCH');

        const events = Array.isArray(payload.events) ? payload.events : [];
        historyState.hasMore = Boolean(payload.has_more);
        historyState.nextCursor = payload.next_cursor || null;
        historyState.summary = payload.summary || null;

        if (!append) {
            historyState.items = [...events];
        } else {
            const seen = new Set(historyState.items.map((x) => x.event_id));
            events.forEach((e) => {
                if (!seen.has(e.event_id)) {
                    historyState.items.push(e);
                    seen.add(e.event_id);
                }
            });
        }

        document.getElementById('error-alert').classList.add('hidden');
        document.getElementById('status-indicator').innerHTML = `<span class="text-green-400">● Обновлено: ${new Date().toLocaleTimeString()}</span>`;

        if (!append) {
            populateHistoryFilters(typeRows, picklists);
        }
        updateKpiCards(historyState.summary);
        renderMainChart(historyState.items);
        renderTypeChartFromApi(typeRows);
        renderTable(historyState.items);

        if (loadMoreBtn) {
            loadMoreBtn.classList.toggle('hidden', !historyState.hasMore);
            loadMoreBtn.disabled = !historyState.hasMore;
        }
    } catch (err) {
        console.error('Dashboard Error:', err);
        const errorText = document.getElementById('error-text');
        if (errorText) errorText.innerText = err.message;
        document.getElementById('error-alert').classList.remove('hidden');
        document.getElementById('status-indicator').innerHTML = '<span class="text-red-500">● Ошибка связи</span>';
    } finally {
        historyLoadLocked = false;
        if (loadMoreBtn) {
            loadMoreBtn.disabled = !historyState.hasMore;
        }
    }
}

window.refreshData = async function refreshData() {
    try {
        const r = await fetch('/api/update');
        if (!r.ok) throw new Error(`update ${r.status}`);
        await loadData({ resetPaging: true });
    } catch (e) {
        console.error(e);
    }
};

function setupDashboardAutoRefresh() {
    if (dashboardRefreshIntervalId) {
        clearInterval(dashboardRefreshIntervalId);
        dashboardRefreshIntervalId = null;
    }
    const sel = document.getElementById('refreshInterval');
    if (!sel) return;
    const val = parseInt(sel.value, 10);
    try {
        localStorage.setItem(LS_REFRESH, String(val));
    } catch (_) { /* ignore */ }
    if (val > 0) {
        dashboardRefreshIntervalId = setInterval(() => {
            loadData({ resetPaging: true });
        }, val);
    }
}



document.addEventListener('DOMContentLoaded', () => {
    const periodSelect = document.getElementById('periodFilter');
    const refreshSelect = document.getElementById('refreshInterval');
    try {
        const savedP = localStorage.getItem(LS_PERIOD);
        if (savedP && periodSelect && [...periodSelect.options].some((o) => o.value === savedP)) {
            periodSelect.value = savedP;
        }
        const savedR = localStorage.getItem(LS_REFRESH);
        if (savedR && refreshSelect && [...refreshSelect.options].some((o) => o.value === savedR)) {
            refreshSelect.value = savedR;
        }
    } catch (_) { /* ignore */ }

    loadData({ resetPaging: true });

    if (periodSelect) {
        periodSelect.addEventListener('change', () => {
            try {
                localStorage.setItem(LS_PERIOD, getSelectedPeriod());
            } catch (_) { /* ignore */ }
            closeErrorTypeDrawer();
            closeAllFilterMultiddPanels();
            loadData({ resetPaging: true });
        });
    }

    initFilterMultiddUi();

    const filterBtn = document.getElementById('filterApplyBtn');
    if (filterBtn) {
        filterBtn.addEventListener('click', () => {
            closeAllFilterMultiddPanels();
            loadData({ resetPaging: true });
        });
    }

    const loadMoreBtn = document.getElementById('historyLoadMore');
    if (loadMoreBtn) {
        loadMoreBtn.addEventListener('click', () => loadData({ append: true, resetPaging: false }));
    }

    const tbody = document.getElementById('tableBody');
    if (tbody) {
        tbody.addEventListener('click', (e) => {
            const btn = e.target.closest('.copy-raw-btn');
            if (!btn) return;
            e.stopPropagation();
            const gi = Number(btn.dataset.groupIndex);
            const ii = Number(btn.dataset.instIndex ?? 0);
            const inst = window.__lastHistoryGroups?.[gi]?.instances?.[ii];
            const text = inst?.raw_message;
            if (text) {
                navigator.clipboard.writeText(text).catch(() => {});
            }
        });
    }

    const backdrop = document.getElementById('errorDetailsBackdrop');
    if (backdrop) {
        backdrop.addEventListener('click', closeErrorTypeDrawer);
    }
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') {
            closeAllFilterMultiddPanels();
            closeErrorTypeDrawer();
        }
    });

    if (refreshSelect) {
        refreshSelect.addEventListener('change', () => setupDashboardAutoRefresh());
    }
    setupDashboardAutoRefresh();
});
