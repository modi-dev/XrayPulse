let refreshTimer = null;
let myChart = null;

/**
 * 1. Сначала определяем вспомогательные функции отрисовки
 */
function renderChart(data) {
    const canvas = document.getElementById('mainChart');
    if (!canvas) return; // Защита, если канваса нет на странице
    
    const ctx = canvas.getContext('2d');
    
    // Группируем данные для графика (считаем количество уникальных типов ошибок)
    const stats = {};
    data.forEach(item => {
        stats[item.type] = (stats[item.type] || 0) + 1;
    });

    const labels = Object.keys(stats).map(label => label.split('>').pop().trim().substring(0, 25));
    const counts = Object.values(stats);

    if (myChart) {
        myChart.destroy();
    }

    myChart = new Chart(ctx, {
        type: 'bar',
        data: {
            labels: labels,
            datasets: [{
                label: 'Инциденты',
                data: counts,
                backgroundColor: 'rgba(59, 130, 246, 0.5)',
                borderColor: 'rgba(59, 130, 246, 1)',
                borderWidth: 1
            }]
        },
        options: {
            indexAxis: 'y',
            responsive: true,
            maintainAspectRatio: false,
            scales: {
                x: { grid: { color: '#374151' }, ticks: { color: '#9CA3AF' } },
                y: { grid: { display: false }, ticks: { color: '#9CA3AF', font: { size: 10 } } }
            },
            plugins: { legend: { display: false } }
        }
    });
}

// Функция группировки данных
function groupLogs(rawData) {
    const groups = [];
    const groupMap = new Map();

    rawData.forEach(item => {
        // Ключ группировки — текст ошибки + описание
        const key = `${item.type}-${item.desc}`;
        
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

function renderTable(rawData) {
    const tbody = document.getElementById('tableBody');
    if (!tbody) return;

    const groupedData = groupLogs(rawData);

    tbody.innerHTML = groupedData.map((group, index) => {
        // Счетчик отображаем справа, если ошибок > 1
        const countBadge = group.count > 1 
            ? `<span class="ml-auto px-2 py-0.5 bg-blue-900/50 text-blue-400 border border-blue-500/30 rounded text-[10px] font-bold">×${group.count}</span>` 
            : '';

        return `
            <tr class="border-b border-gray-700 hover:bg-gray-750 transition cursor-pointer group" onclick="toggleDetails(${index})">
                <td class="px-4 py-3 text-xs text-blue-300 font-mono">${group.timestamp}</td>
                <td class="px-4 py-3 text-xs text-green-400 font-mono">${group.source}</td>
                <td class="px-4 py-3 text-xs text-yellow-200 font-mono">${group.destination}</td>
                <td class="px-4 py-3 text-xs text-red-400 font-mono">
                    <div class="flex items-center w-full">
                        <span class="truncate max-w-[200px] md:max-w-xs">${group.type}</span>
                        ${countBadge}
                    </div>
                </td>
                <td class="px-4 py-3 text-sm text-gray-300">${group.desc}</td>
            </tr>
            
            <tr id="details-${index}" class="hidden bg-black/30 border-b border-gray-800">
                <td colspan="5" class="px-6 py-4">
                    <div class="text-[11px] text-gray-400 mb-2 uppercase font-bold tracking-wider">Все вхождения в этой группе:</div>
                    <div class="max-h-40 overflow-y-auto space-y-1 pr-2 custom-scrollbar">
                        ${group.instances.map(inst => `
                            <div class="flex justify-between items-center py-1 border-b border-gray-800/50 last:border-0 hover:text-gray-200">
                                <span class="font-mono text-blue-400/80 w-32">${inst.timestamp.split(' ')[1]}</span>
                                <span class="font-mono text-green-500/70 w-32">${inst.source}</span>
                                <span class="font-mono text-gray-500 truncate flex-1 ml-4">${inst.type}</span>
                            </div>
                        `).join('')}
                    </div>
                </td>
            </tr>
        `;
    }).join('');
}

// Глобальная функция для переключения видимости
window.toggleDetails = function(index) {
    const el = document.getElementById(`details-${index}`);
    if (el) el.classList.toggle('hidden');
};

/**
 * 2. Основная функция загрузки (теперь она "видит" функции выше)
 */
async function loadData() {
    const errorAlert = document.getElementById('error-alert');
    const errorText = document.getElementById('error-text');
    const statusIndicator = document.getElementById('status-indicator');
    
    try {
        const response = await fetch('/api/history');
        if (!response.ok) throw new Error(`Ошибка сервера: ${response.status}`);
        
        const data = await response.json();
        
        errorAlert.classList.add('hidden');
        statusIndicator.innerHTML = `<span class="text-green-400">● Обновлено: ${new Date().toLocaleTimeString()}</span>`;
        
        // Теперь функции точно определены
        renderChart(data);
        renderTable(data);
        
    } catch (err) {
        console.error("Dashboard Error:", err);
        if (errorText) errorText.innerText = err.message;
        if (errorAlert) errorAlert.classList.remove('hidden');
        if (statusIndicator) statusIndicator.innerHTML = '<span class="text-red-500">● Ошибка связи</span>';
    }
}

// Привязываем к глобальному окну, чтобы onclick в HTML работал
window.loadData = loadData;

/**
 * 3. Настройка автообновления и инициализация
 */
document.addEventListener('DOMContentLoaded', () => {
    const select = document.getElementById('refreshInterval');
    
    loadData(); // Первый запуск

    select.addEventListener('change', (e) => {
        const val = parseInt(e.target.value);
        if (refreshTimer) clearInterval(refreshTimer);
        if (val > 0) {
            refreshTimer = setInterval(loadData, val);
        }
    });

    // Установка интервала по умолчанию (30 сек)
    refreshTimer = setInterval(loadData, 30000);
});