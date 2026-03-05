$(document).ready(function () {

    $("#toggleSidebar").click(function () {
        $("#sidebar").toggleClass("show");
        $("#sidebarOverlay").fadeToggle(200);
    });

    /* Overlay click closes sidebar */
    $("#sidebarOverlay").click(function () {
        closeSidebar();
    });

    /* DROPDOWN TOGGLE (IMPORTANT FIX) */
    $(".menu-toggle").click(function (e) {
        e.stopPropagation(); // ⛔ STOP sidebar close

        $(".submenu").not($(this).next()).slideUp(200);
        $(".menu-toggle").not($(this)).removeClass("active");

        $(this).toggleClass("active");
        $(this).next(".submenu").slideToggle(200);
    });

    /* Submenu item click → close sidebar (mobile only) */
    $(".submenu a").click(function () {
        if ($(window).width() < 769) {
            closeSidebar();
        }
    });

    /* Normal menu click → close sidebar (mobile only) */
    $(".sidebar > a:not(.menu-toggle)").click(function () {
        if ($(window).width() < 769) {
            closeSidebar();
        }
    });

    function closeSidebar() {
        $("#sidebar").removeClass("show");
        $("#sidebarOverlay").fadeOut(200);
    }

});
$(document).ready(function () {

    setTimeout(function () {
        $(".alert").slideUp("slow", function () {
            $(this).alert('close');   // fully remove from DOM
        });
    }, 3000); // 5000ms = 5 sec

});