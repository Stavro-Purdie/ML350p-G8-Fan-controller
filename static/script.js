setInterval(() => {
  fetch('/status')
    .then(res => res.json())
    .then(data => {
      document.getElementById('cpu').innerText = data.cpu;
      document.getElementById('gpu').innerText = data.gpu;
      const fanList = document.getElementById('fan-speeds');
      fanList.innerHTML = '';
      data.fans.forEach(f => {
        let li = document.createElement('li');
        li.innerText = f + '%';
        fanList.appendChild(li);
      });
    });
}, 5000); // update every 5 seconds
