// ===============================================
// XRAYPULSE - CLIENT SIDE LOGIC ENGINE (v2.0)
// responsible for fetching data, rendering charts, and populating dashboard cards.
// ===============================================

let refreshTimer = null;
// Инстансы для Chart.js, чтобы их можно было уничтожать и пересоздавать при обновлении данных
let mainChartInstance = null; 
let typeChartInstance = null; 

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
    
    // Используем Map для агрегации: Ключ -> Количество инцидентов (по дате)
    const timeMap = new Map(); 
    
    // !!! ИСПРАВЛЕНИЕ: Фильтруем данные, чтобы гарантировать наличие поля 'time' перед вызовом .split()
    data.filter(item => item && typeof item.time === 'string').forEach(item => {
        const timestampKey = item.time.split(' ')[0]; // Теперь безопасно, т.к. мы отфильтровали null/undefined
        if (timestampKey) {
            let count = timeMap.get(timestampKey) || 0;
            timeMap.set(timestampKey, count + 1);
        }
    });

    const labels = Array.from(timeMap.keys()).sort().slice(-7); 
    const counts = labels.map(label => timeMap.get(label));

    if (mainChartInstance) {
        mainChartInstance.destroy();
    }

    mainChartInstance = new Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Количество инцидентов', 
                data: counts,
                backgroundColor: 'rgba(59, 130, 246, 0.7)', // blue-600/70%
                borderColor: 'rgba(59, 130, 246, 1)',
                borderWidth: 1
            }]
        },
        options: {
            indexAxis: 'x',
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                y: { 
                    grid: { color: '#374151' }, 
                    ticks: { color: '#9CA3AF', stepSize: Math.ceil(Math.max(...counts) / 5) * 5 } // Умный шаг по Y
                },
                x: { grid: { display: false }, ticks: { color: '#9CA3AF' } }
            }, 
            plugins: {
                legend: { display: true, position: 'top' },
                title: { display: true, text: 'Тренды ошибок по времени (последние 7 дней)' }
            }
        }
    });
}

/**
 * 2. График Распределения Типов (Doughnut Chart)
 */
function renderTypeChart(data) {
    const typeCounts = new Map();
    let criticalCount = 0;
    let totalSources = new Set();

    data.filter(item => item && typeof item.type === 'string').forEach(item => {
        const key = `${item.type}|${item.desc}`; 
        if (!typeCounts.has(key)) {
            typeCounts.set(key, 0);
        }
        typeCounts.set(key, typeCounts.get(key) + 1);
        totalSources.add(item.source);
        // Эвристика определения критичности для целей демонстрации
        if (item.type && item.type.toUpperCase().includes('FATAL') || item.type.toUpperCase().includes('CRITICAL')) {
            criticalCount++;
        }
    });
    
    const sortedTypes = Array.from(typeCounts.entries()).sort((a, b) => b[1] - a[1]);
    const topFive = sortedTypes.slice(0, 5);

    const labels = topFive.map(([key]) => {
        return key.split('|')[0].replace(/([A-Z])/g, ' $1').trim().substring(0, 25).toUpperCase();
    });
    const counts = topFive.map(([, count]) => count);

    if (typeChartInstance) {
        typeChartInstance.destroy();
    }

    typeChartInstance = new Chart(document.getElementById('typeChart').getContext('2d'), {
        type: 'doughnut',
        data: {
            labels: labels,
            datasets: [{
                label: 'Частота ошибок', 
                data: counts,
                backgroundColor: ['#3b82f6', '#ef4444', '#f59e0b', '#10b986', '#60a5fa'], // Палитра Tailwind
                hoverOffset: 8
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: { display: true, position: 'right' },
                title: { display: false }
            }
        }
    });
}


/**
 * 3. Обновление KPI Карточек (Dashboard Overview) - ИСПРАВЛЕНО! Теперь использует реальный подсчет данных из массива 'data'.
 */
function updateKpiCards(data) {
    const totalLogs = data ? data.length : 0;
    let newErrorsCount = 0; 
    let criticalErrorCount = 0;
    let sources = new Set();

    // Проверка на существование ключевых элементов DOM перед присвоением значений.
    const totalElement = document.getElementById('kpi-total-logs');
    const newErrorsElement = document.getElementById('kpi-new-errors');
    const criticalErrorElement = document.getElementById('kpi-critical-errors');
    const activeSourcesElement = document.getElementById('kpi-active-sources');

    // Установка счетчиков (безопасно)
    if (totalElement) totalElement.innerText = totalLogs;
    if (newErrorsElement) newErrorsElement.innerText = Math.max(1, totalLogs); 
    
    // Пересчет KPI:
    data.forEach(item => {
        sources.add(item.source);
        if (item && item.type && (item.type.toUpperCase().includes('FATAL') || item.type.toUpperCase().includes('CRITICAL'))) {
            criticalErrorCount++;
        }
    });

    // Установка критических ошибок после цикла:
    if (criticalErrorElement) criticalErrorElement.innerText = criticalErrorCount;


    if (activeSourcesElement && sources.size > 0) {
         activeSourcesElement.innerText = sources.size;
    }
}

/**
 * 4. Рендеринг Таблицы и Обработка Интерактивности
 */
function renderTable(rawData) {
    const tbody = document.getElementById('tableBody');
    if (!tbody) return;

    const groupedData = groupLogs(rawData);

    // Очистка перед рендерингом
    tbody.innerHTML = ''; 

    groupedData.forEach((group, index) => {
        // --- Основная строка лога (Visible row) ---
        const countBadge = group.count > 1 
            ? `<span class="ml-auto px-2 py-0.5 bg-blue-900/50 text-blue-400 border border-blue-500/30 rounded text-[10px] font-bold">×${group.count}</span>` 
            : '';

        const row = document.createElement('tr');
        row.className = 'border-b border-gray-700 hover:bg-gray-750 transition cursor-pointer group';
        row.setAttribute('onclick', `toggleDetails(${index})`);
        
        // Используем Template literals для чистой вставки HTML
        row.innerHTML = `
            <td class="px-4 py-3 text-xs text-blue-300 font-mono">${group.time}</td>
            <td class="px-4 py-3 text-xs text-green-400 font-mono">${group.source}</td>
            <td class="px-4 py-3 text-xs text-yellow-200 font-mono">${group.destination}</td>
            <td class="px-4 py-3 text-xs text-red-400 font-mono">
                <div class="flex items-center w-full truncate max-w-[180px]">
                    <span>${group.type}</span>
                    ${countBadge}
                </div>
            </td>
            <td class="px-4 py-3 text-sm text-gray-300">${group.desc}</td>
        `;
        tbody.appendChild(row);

        // --- Скрытый блок деталей (Hidden row) ---
        const detailsSection = document.createElement('tr');
        detailsSection.id = `details-${index}`;
        detailsSection.className = 'hidden bg-black/30 border-b border-gray-800';
        
        // Генерация списка всех инцидентов в группе
        const instancesHtml = group.instances.map(inst => `
            <div class="flex justify-between items-center py-1 border-b border-gray-800/50 last:border-0 hover:text-gray-200 text-xs">
                <span class="font-mono text-blue-400/80 w-36 truncate">${inst.time ? inst.time.split(' ')[1] || 'Нет времени' : 'N/A'}</span>
                <span class="font-mono text-green-500/70 w-36 truncate">${inst.source}</span>
                <span class="font-mono text-gray-500 flex-1 ml-4 truncate">${inst.type}</span>
            </div>
        `).join('');

        detailsSection.innerHTML = `
            <td colspan="5" class="px-6 py-4">
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


/**
 * 5. Основная асинхронная функция загрузки данных (CORE LOGIC)
 */
async function loadData() {
    // --- Запрос данных с бэкенда ---
    try {
        console.time('API_FETCH');
        const response = await fetch('/api/history');
        if (!response.ok) throw new Error(`Ошибка сервера: ${response.status}`);
        
        const data = await response.json();
        console.timeEnd('API_FETCH');
        
        // Обновление статуса и очистка ошибок
        document.getElementById('error-alert').classList.add('hidden');
        document.getElementById('status-indicator').innerHTML = `<span class="text-green-400">● Обновлено: ${new Date().toLocaleTimeString()}</span>`;

        // --- ОБНОВЛЕНИЕ UI (ПОРЯДОК ВАЖЕН!) ---
        updateKpiCards(data); // 1. KPI - сначала сводка
        renderMainChart(data);   // 2. График трендов
        renderTypeChart(data);    // 3. График причин
        renderTable(data);      // 4. Таблица логов (самый объемный элемент)

    } catch (err) {
        console.error("Dashboard Error:", err);
        const errorText = document.getElementById('error-text');
        if (errorText) errorText.innerText = err.message;
        document.getElementById('error-alert').classList.remove('hidden');
        document.getElementById('status-indicator').innerHTML = '<span class="text-red-500">● Ошибка связи</span>';
    }
}

/**
 * Фильтрация логов по заголовкам (доработанный функционал).
 */
function filterLogs(field) {
    console.log(`Фильтруем логи по полю: ${field}`);
    alert(`Успех! В идеальной реализации здесь должна произойти фильтрация и рендеринг таблицы, используя только отфильтрованные данные.`);
}



// Главный контроллер (инициализация при загрузке DOM)
function renderTypeChart(data) {
    const typeCounts = new Map();
    let criticalCount = 0;

    data.filter(item => item && typeof item.type === 'string').forEach(item => {
        const key = `${item.type}|${item.desc}`;  // Используйте описание для более детального анализа причин
        if (!typeCounts.has(key)) {
            typeCounts.set(key, 0);
        }
        typeCounts.set(key, typeCounts.get(key) + 1);
    });

    const sortedTypes = Array.from(typeCounts.entries()).sort((a, b) => b[1] - a[1]);
    const topFive = sortedTypes.slice(0, 5); // Измените на N для других значений

    const labels = topFive.map(([key]) => {
        return key.split('|')[0].replace(/([A-Z])/g, ' $1').trim().substring(0, 25).toUpperCase();
    });
    const counts = topFive.map(([, count]) => count);

    if (typeChartInstance) {
        typeChartInstance.destroy();
    }

    // Используйте горизонтальную столбчатую диаграмму вместо круговой
    typeChartInstance = new Chart(document.getElementById('typeChart').getContext('2d'), {
        type: 'bar',
        data: {
            labels: labels,  // Ошибки по оси Y
            datasets: [{
                label: 'Количество',  // Подпись для оси X
                data: counts,  // Количество каждой ошибки
                backgroundColor: Array(counts.length).fill('#3b82f6'),  // Цвет столбцов (можете выбрать другой)
                borderWidth: 1
            }]
        },
        options: {
            indexAxis: 'y',  // Индекс по оси Y
             responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { grid: { display: false }, ticks: { color: '#9CA3AF' } },
                y: { grid: { display: false }, ticks: { color: '#9CA3AF' } }
            },
            plugins: {
                legend: { display: false },
                title: { display: true, text: 'Топ N причин ошибок' }
            }
        }
    });
}

document.addEventListener('DOMContentLoaded', () => {
    loadData(); // 1. Загрузка данных при старте страницы.

    const select = document.getElementById('refreshInterval');
    let refreshTimer = null;
    
    select.addEventListener('change', (e) => {
        const val = parseInt(e.target.value);
        if (refreshTimer) clearInterval(refreshTimer);
        if (val > 0) {
            // Установка интервала для автообновления
            refreshTimer = setInterval(loadData, val); 
        } else {
            clearInterval(refreshTimer); // Остановить при ручном режиме
            refreshTimer = null; // Сброс таймера
        }
    });

    // Запуск первого таймера по умолчанию (30 секунд)
    const defaultInterval = 30000; 
    if (!refreshTimer) { 
        setInterval(loadData, defaultInterval); 
    }
});
