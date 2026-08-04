const puppeteer = require('puppeteer-extra');
const StealthPlugin = require('puppeteer-extra-plugin-stealth');
puppeteer.use(StealthPlugin());

(async () => {
  const browser = await puppeteer.launch({ headless: 'new' });
  const page = await browser.newPage();
  
  await page.goto('https://cardtell.id/search?taxonomy_id=57', { waitUntil: 'networkidle2' });
  
  // Wait for products to load
  try {
    await new Promise(r => setTimeout(r, 5000)); // just wait 5 seconds to be safe
    
    // Evaluate and extract all text or elements to see what is there
    const content = await page.evaluate(() => {
      return document.body.innerText;
    });
    
    const html = await page.evaluate(() => {
      return document.body.innerHTML;
    });

    console.log("TEXT CONTENT (Snippet):");
    console.log(content.substring(0, 1000));
    
    const fs = require('fs');
    fs.writeFileSync('cardtell_test_output.html', html);
    
  } catch (err) {
    console.error(err);
  }
  
  await browser.close();
})();
